r"""
媒体文件智能索引器 — 通用版
============================
用视觉大模型(默认阿里百炼 qwen3-vl-plus，可选智谱 glm-4.6v-flash)
分析指定文件夹的所有图片和视频，生成结构化描述存入 SQLite FTS5 数据库，
支持 jieba 分词秒级检索。

用法:
  python media_indexer.py --root "D:\SomeFolder" [--db PATH] [--workers N]
                         [--provider dashscope|zhipu] [--model MODEL]
                         [--limit N] [--shuffle]

依赖: Pillow, jieba, ffmpeg/ffprobe, DASHSCOPE_API_KEY 或 GLM_API_KEY in ~/.hermes/.env
"""
from __future__ import annotations
import argparse
import base64
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Lock

import http.client

# ── 默认配置 ────────────────────────────────────────────────────────────
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tiff", ".tif"}
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm"}

MAX_WORKERS = 3
API_TIMEOUT = 30
MAX_RETRIES = 3
MAX_IMAGE_PX = 1024
VIDEO_KEYFRAMES = 3
BATCH_LOG_EVERY = 50

# ── 视觉模型 Provider ────────────────────────────────────────────────
PROVIDERS = {
    "dashscope": {  # 阿里百炼（默认）
        "host": "dashscope.aliyuncs.com",
        "path": "/compatible-mode/v1/chat/completions",
        "env_key": "DASHSCOPE_API_KEY",
        "default_model": "qwen3-vl-plus",
    },
    "zhipu": {  # 智谱（原默认）
        "host": "open.bigmodel.cn",
        "path": "/api/paas/v4/chat/completions",
        "env_key": "GLM_API_KEY",
        "default_model": "glm-4.6v-flash",
    },
}
PROVIDER = "dashscope"   # 全局默认 provider（--provider 覆盖）
MODEL = None             # 全局模型（None 时用 provider 默认模型）
MIN_IMAGE_PX = 128       # qwen3-vl 拒绝过小图片(<100px 会 400)，小于此值放大
TOTAL_PROMPT_TOKENS = 0          # 成本统计（多线程累加，仅估算）
TOTAL_COMPLETION_TOKENS = 0
TOKEN_LOCK = Lock()


def setup_logging(log_path: str):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("media_indexer")


# ── 数据库 ──────────────────────────────────────────────────────────────
def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS media_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT UNIQUE NOT NULL,
            file_name TEXT NOT NULL,
            file_ext TEXT NOT NULL,
            file_size INTEGER,
            file_mtime TEXT,
            file_hash TEXT,
            media_type TEXT NOT NULL,
            description TEXT,
            tags TEXT,
            folder_path TEXT,
            duration_seconds REAL,
            width INTEGER,
            height INTEGER,
            segmented_text TEXT DEFAULT '',
            indexed_at TEXT NOT NULL,
            error TEXT
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS media_search USING fts5(
            file_path, file_name, description, tags, folder_path, segmented_text,
            content=media_files, content_rowid=id
        )
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS media_ai AFTER INSERT ON media_files BEGIN
            INSERT INTO media_search(rowid, file_path, file_name, description, tags, folder_path, segmented_text)
            VALUES (new.id, new.file_path, new.file_name, new.description, new.tags, new.folder_path, new.segmented_text);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS media_ad AFTER DELETE ON media_files BEGIN
            INSERT INTO media_search(media_search, rowid, file_path, file_name, description, tags, folder_path, segmented_text)
            VALUES ('delete', old.id, old.file_path, old.file_name, old.description, old.tags, old.folder_path, old.segmented_text);
        END
    """)
    conn.commit()
    return conn


def is_already_indexed(conn, file_path: str, mtime: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM media_files WHERE file_path=? AND file_mtime=? AND error IS NULL",
        (file_path, mtime),
    ).fetchone()
    return row is not None


# ── 视觉 API ─────────────────────────────────────────────────────────
def _get_api_key(env_key: str) -> str:
    key = os.environ.get(env_key, "").strip()
    if key:
        return key
    env_candidates = [
        os.path.expanduser("~/.hermes/.env"),
        r"D:\@佳康顺矩阵\hermes\.env",
    ]
    for env_path in env_candidates:
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith(env_key):
                        key = line.split("=", 1)[1].strip().strip('"').strip("'")
                        if key:
                            return key
    raise RuntimeError(f"{env_key} not found in env or {env_candidates}")


def call_vision(prompt: str, image_base64: str = None, image_url: str = None,
                provider: str = None, model: str = None) -> str:
    """调用视觉模型(OpenAI 兼容格式)，provider 见 PROVIDERS 字典"""
    global TOTAL_PROMPT_TOKENS, TOTAL_COMPLETION_TOKENS
    provider = provider or PROVIDER
    if provider not in PROVIDERS:
        raise RuntimeError(f"未知 provider: {provider}，可选 {list(PROVIDERS.keys())}")
    cfg = PROVIDERS[provider]
    api_key = _get_api_key(cfg["env_key"])
    model = model or MODEL or cfg["default_model"]

    content = [{"type": "text", "text": prompt}]
    if image_base64:
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}})
    elif image_url:
        content.append({"type": "image_url", "image_url": {"url": image_url}})

    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": 500,
        "temperature": 0.1,
    }

    for attempt in range(MAX_RETRIES):
        try:
            c = http.client.HTTPSConnection(cfg["host"], timeout=API_TIMEOUT)
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            c.request("POST", cfg["path"], body=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers)
            resp = c.getresponse()
            raw = resp.read().decode("utf-8")
            c.close()
            data = json.loads(raw)
            if "error" in data:
                err_msg = data["error"].get("message", str(data["error"]))
                if "rate" in err_msg.lower() or "limit" in err_msg.lower() or "throttl" in err_msg.lower():
                    time.sleep(5 * (attempt + 1))
                    continue
                raise RuntimeError(f"API error: {err_msg[:300]}")
            choices = data.get("choices", [])
            if choices:
                usage = data.get("usage", {})
                if usage:
                    with TOKEN_LOCK:
                        TOTAL_PROMPT_TOKENS += usage.get("prompt_tokens", 0)
                        TOTAL_COMPLETION_TOKENS += usage.get("completion_tokens", 0)
                return choices[0].get("message", {}).get("content", "").strip()
            raise RuntimeError(f"Empty choices: {raw[:300]}")
        except (http.client.HTTPException, ConnectionError, TimeoutError) as e:
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"Failed after {MAX_RETRIES} attempts")


def call_glm4v_flash(prompt: str, image_base64: str = None, image_url: str = None) -> str:
    """兼容旧调用：等价于 call_vision(..., provider='zhipu')"""
    return call_vision(prompt, image_base64=image_base64, image_url=image_url, provider="zhipu")


# ── JSON解析 ───────────────────────────────────────────────────────────
def parse_json_response(text: str) -> dict:
    """解析模型返回的JSON，兼容markdown代码块包裹、details为dict/list等"""
    def _safe_str(v):
        """递归展平任意嵌套结构为字符串"""
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            return ", ".join(_safe_str(x) for x in v)
        if isinstance(v, dict):
            parts = [_safe_str(val) for val in v.values() if _safe_str(val)]
            return "; ".join(parts)
        return str(v) if v is not None else ""
    try:
        t = text.strip()
        if t.startswith("```"):
            t = re.sub(r'^```\w*\n?', '', t)
            t = re.sub(r'\n?```$', '', t)
            t = t.strip()
        m = re.search(r'\{.*\}', t, re.DOTALL)
        if m:
            parsed = json.loads(m.group())
            parsed["details"] = _safe_str(parsed.get("details", ""))
            for f in ["description", "style", "colors"]:
                if not isinstance(parsed.get(f, ""), str):
                    parsed[f] = _safe_str(parsed.get(f, "", ""))
            return parsed
    except json.JSONDecodeError:
        pass
    return {"description": text, "details": "", "style": "", "colors": ""}


# ── 图片处理 ────────────────────────────────────────────────────────────
def resize_image_to_base64(file_path: str, max_px: int = MAX_IMAGE_PX) -> tuple:
    from PIL import Image
    import io
    img = Image.open(file_path)
    orig_w, orig_h = img.size
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if max(orig_w, orig_h) > max_px:
        ratio = max_px / max(orig_w, orig_h)
        img = img.resize((int(orig_w * ratio), int(orig_h * ratio)), Image.LANCZOS)
    elif max(orig_w, orig_h) < MIN_IMAGE_PX:
        # qwen3-vl 拒绝过小图片(如 1x1 纯色会 HTTP 400)，放大到最小边
        ratio = MIN_IMAGE_PX / max(orig_w, orig_h)
        img = img.resize((max(1, int(orig_w * ratio)), max(1, int(orig_h * ratio))), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("ascii"), (orig_w, orig_h)


IMAGE_PROMPT = """请仔细观察这张图片，提供以下信息：

1. **描述**: 用1-2句话概括图片主要内容
2. **细节**: 列出图片中的关键物体、人物、场景、文字等（每个用逗号分隔）
3. **风格**: 图片风格（如: 实拍照片/插画/截图/海报/文档/产品图/风景等）
4. **色彩**: 主色调

请严格用以下JSON格式回答（不要其他内容）:
{"description":"...", "details":"...", "style":"...", "colors":"..."}"""


def analyze_image(file_path: str) -> dict:
    b64, (w, h) = resize_image_to_base64(file_path)
    result_text = call_vision(IMAGE_PROMPT, image_base64=b64)
    parsed = parse_json_response(result_text)
    return {
        "description": parsed.get("description", ""),
        "tags": parsed.get("details", ""),
        "style": parsed.get("style", ""),
        "colors": parsed.get("colors", ""),
        "width": w, "height": h,
    }


# ── 视频处理 ────────────────────────────────────────────────────────────
def extract_keyframes(video_path: str, num_frames: int = VIDEO_KEYFRAMES):
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", video_path],
            capture_output=True, text=True, timeout=15,
        )
        info = json.loads(probe.stdout)
        duration = float(info.get("format", {}).get("duration", 0))
        width = height = 0
        for s in info.get("streams", []):
            if s.get("codec_type") == "video":
                width, height = int(s.get("width", 0)), int(s.get("height", 0))
                break
    except Exception:
        return [], 0, 0, 0

    if duration <= 0:
        return [], 0, 0, 0

    timestamps = [duration * (i + 1) / (num_frames + 1) for i in range(num_frames)]
    frames = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for i, ts in enumerate(timestamps):
            out_path = os.path.join(tmpdir, f"frame_{i:03d}.jpg")
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", str(ts), "-i", video_path,
                     "-frames:v", "1", "-q:v", "2",
                     "-vf", f"scale='min(1024,iw)':'min(1024,ih)':force_original_aspect_ratio=decrease",
                     out_path],
                    capture_output=True, timeout=15,
                )
                if os.path.exists(out_path):
                    b64, (fw, fh) = resize_image_to_base64(out_path)
                    frames.append((b64, ts))
            except Exception:
                pass
    return frames, duration, width, height


VIDEO_FRAME_PROMPT = "这是视频第 {time_str} 的截图。请简短描述这一帧的内容（1句话以内），重点描述画面中的人物、物体、动作和场景。"

VIDEO_SUMMARY_PROMPT = """以下是一个视频的 {count} 个关键帧描述：
{frame_descriptions}

请综合以上信息，提供这个视频的整体描述：

1. **描述**: 用1-2句话概括视频主要内容
2. **细节**: 列出视频中的关键物体、人物、场景、动作等
3. **风格**: 视频风格（如: 实拍/动画/口播/产品展示/纪录片/教学等）

请严格用以下JSON格式回答:
{{"description":"...", "details":"...", "style":"..."}}

注意：细节中请包含视频中出现的任何文字、品牌名、产品名等。"""


def analyze_video(file_path: str, log=None) -> dict:
    frames, duration, width, height = extract_keyframes(file_path)
    if not frames:
        return {"description": "无法提取视频关键帧", "tags": "", "style": "",
                "duration": duration, "width": width, "height": height,
                "error": "frame_extraction_failed"}

    frame_descriptions = []
    for b64, ts in frames:
        time_str = f"{int(ts//60):02d}:{int(ts%60):02d}"
        prompt = VIDEO_FRAME_PROMPT.format(time_str=time_str)
        desc = call_vision(prompt, image_base64=b64)
        frame_descriptions.append(f"[{time_str}] {desc}")
        time.sleep(0.3)

    frame_text = "\n".join(frame_descriptions)
    summary_prompt = VIDEO_SUMMARY_PROMPT.format(count=len(frames), frame_descriptions=frame_text)
    summary_text = call_vision(summary_prompt)
    parsed = parse_json_response(summary_text)

    return {
        "description": parsed.get("description", ""),
        "tags": parsed.get("details", ""),
        "style": parsed.get("style", ""),
        "duration": duration, "width": width, "height": height,
    }


# ── 文件扫描 ────────────────────────────────────────────────────────────
def scan_files(root: str) -> list:
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "node_modules" and d != "__pycache__"]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext in IMAGE_EXTS or ext in VIDEO_EXTS:
                full = os.path.join(dirpath, fname)
                try:
                    stat = os.stat(full)
                    mtime = datetime.fromtimestamp(stat.st_mtime).isoformat()
                    media_type = "image" if ext in IMAGE_EXTS else "video"
                    files.append((full, ext, stat.st_size, mtime, media_type))
                except OSError:
                    continue
    return files


# ── jieba 分词 ─────────────────────────────────────────────────────────
def segment_text(text: str) -> str:
    try:
        import jieba
        return " ".join(jieba.cut(text))
    except ImportError:
        return text


# ── 处理单文件 ─────────────────────────────────────────────────────────
def process_single_file(item, conn, lock, log):
    file_path, ext, size, mtime, media_type = item
    with lock:
        if is_already_indexed(conn, file_path, mtime):
            return {"status": "skipped", "path": file_path}

    try:
        if media_type == "image":
            result = analyze_image(file_path)
        else:
            result = analyze_video(file_path, log)

        seg_input = f"{os.path.basename(file_path)} {result.get('description','')} {result.get('tags','')} {os.path.dirname(file_path)}"
        segmented = segment_text(seg_input)

        with lock:
            conn.execute("""
                INSERT OR REPLACE INTO media_files
                (file_path, file_name, file_ext, file_size, file_mtime, file_hash,
                 media_type, description, tags, folder_path, duration_seconds,
                 width, height, segmented_text, indexed_at, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                file_path, os.path.basename(file_path), ext, size, mtime, "",
                media_type, result.get("description", ""), result.get("tags", ""),
                os.path.dirname(file_path), result.get("duration", 0),
                result.get("width", 0), result.get("height", 0), segmented,
                datetime.now().isoformat(), result.get("error"),
            ))
            conn.commit()
        return {"status": "ok", "path": file_path, "type": media_type}

    except Exception as e:
        err_msg = f"{type(e).__name__}: {str(e)[:300]}"
        if log:
            log.error(f"Failed: {file_path} - {err_msg}")
        with lock:
            conn.execute("""
                INSERT OR REPLACE INTO media_files
                (file_path, file_name, file_ext, file_size, file_mtime, file_hash,
                 media_type, description, tags, folder_path, duration_seconds,
                 width, height, segmented_text, indexed_at, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                file_path, os.path.basename(file_path), ext, size, mtime, "",
                media_type, "", "", os.path.dirname(file_path), 0, 0, 0, "",
                datetime.now().isoformat(), err_msg,
            ))
            conn.commit()
        return {"status": "error", "path": file_path, "error": err_msg}


# ── 主流程 ──────────────────────────────────────────────────────────────
def main():
    # Override globals（必须最先声明，argparse 默认值会读取模块全局）
    global MAX_IMAGE_PX, VIDEO_KEYFRAMES, MAX_WORKERS, PROVIDER, MODEL

    parser = argparse.ArgumentParser(description="媒体文件智能索引器")
    parser.add_argument("--root", required=True, help="扫描根目录")
    parser.add_argument("--db", default=None, help="数据库路径 (默认: <root>/media_index.db)")
    parser.add_argument("--workers", type=int, default=MAX_WORKERS, help="并发线程数")
    parser.add_argument("--max-image-px", type=int, default=MAX_IMAGE_PX, help="图片缩放最大边长")
    parser.add_argument("--video-keyframes", type=int, default=VIDEO_KEYFRAMES, help="视频关键帧数")
    parser.add_argument("--provider", choices=list(PROVIDERS.keys()), default=PROVIDER,
                        help="视觉模型厂商 (dashscope=阿里百炼默认 / zhipu=智谱)")
    parser.add_argument("--model", default=None, help="视觉模型名 (默认取 provider 默认模型)")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个未索引文件 (0=全部，用于分批/试跑)")
    parser.add_argument("--shuffle", action="store_true", help="随机打乱处理顺序 (试跑取样用)")
    parser.add_argument("--images-only", action="store_true", help="只处理图片，跳过视频")
    args = parser.parse_args()

    scan_root = os.path.abspath(args.root)
    db_path = args.db or os.path.join(os.path.dirname(scan_root), "media_index.db")
    log_path = os.path.join(os.path.dirname(db_path), "indexer.log")

    # Override globals（已在上方声明）
    MAX_IMAGE_PX = args.max_image_px
    VIDEO_KEYFRAMES = args.video_keyframes
    MAX_WORKERS = args.workers
    PROVIDER = args.provider
    MODEL = args.model

    log = setup_logging(log_path)
    start = time.time()

    log.info("=" * 60)
    log.info("媒体文件智能索引器启动")
    log.info(f"扫描根目录: {scan_root}")
    log.info(f"数据库: {db_path}")
    log.info(f"并发: {MAX_WORKERS} 线程")
    log.info(f"视觉模型: {PROVIDERS[PROVIDER]['env_key']} / {MODEL or PROVIDERS[PROVIDER]['default_model']}")
    log.info("=" * 60)

    conn = init_db(db_path)
    lock = Lock()

    log.info("正在扫描文件...")
    files = scan_files(scan_root)
    images = [f for f in files if f[4] == "image"]
    videos = [f for f in files if f[4] == "video"]
    if args.images_only:
        files = images
        videos = []
        log.info("--images-only: 只处理图片，跳过视频")
    log.info(f"发现 {len(images)} 张图片, {len(videos)} 个视频 (共 {len(files)} 个文件)")

    already = sum(1 for f in files if is_already_indexed(conn, f[0], f[3]))
    log.info(f"已索引: {already}, 待处理: {len(files) - already}")

    files.sort(key=lambda f: (0 if f[4] == "image" else 1, f[2]))

    if args.shuffle:
        import random
        random.shuffle(files)
    if args.limit and args.limit > 0:
        files = files[: args.limit]
        log.info(f"分批模式: 本次只处理前 {args.limit} 个文件")

    done = ok_count = err_count = skip_count = 0
    total = len(files)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(process_single_file, f, conn, lock, log): f for f in files}
        for future in as_completed(futures):
            result = future.result()
            done += 1
            if result["status"] == "ok":
                ok_count += 1
            elif result["status"] == "skipped":
                skip_count += 1
            else:
                err_count += 1
            if done % BATCH_LOG_EVERY == 0 or done == total:
                elapsed = time.time() - start
                rate = done / elapsed if elapsed > 0 else 0
                remaining = (total - done) / rate if rate > 0 else 0
                log.info(
                    f"进度: {done}/{total} ({done*100//total}%) "
                    f"| 成功:{ok_count} 跳过:{skip_count} 失败:{err_count} "
                    f"| 速度:{rate:.1f}个/分 | 预计剩余:{remaining/60:.0f}分钟"
                )

    elapsed = time.time() - start
    log.info("=" * 60)
    log.info(f"索引完成! 耗时: {elapsed/60:.1f}分钟")
    log.info(f"总计: {total}, 成功: {ok_count}, 跳过: {skip_count}, 失败: {err_count}")
    if TOTAL_PROMPT_TOKENS or TOTAL_COMPLETION_TOKENS:
        log.info(f"Token 消耗: 输入 {TOTAL_PROMPT_TOKENS:,} / 输出 {TOTAL_COMPLETION_TOKENS:,} "
                 f"(约 {TOTAL_PROMPT_TOKENS + TOTAL_COMPLETION_TOKENS:,} tokens, 费用以百炼账单为准)")
    log.info(f"数据库: {db_path}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
