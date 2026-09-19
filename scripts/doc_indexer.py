# -*- coding: utf-8 -*-
r"""
全文件智能索引器 — gongzuo 扩展
================================
扫描指定文件夹，对【所有非媒体文件】提取正文内容（txt/md/pdf/docx/xlsx/pptx/
doc/xls/epub/xmind/代码/配置…），图片/视频/音频只收录元数据（文件名/路径可搜），
统一存入 SQLite FTS5(trigram) 数据库，实现中文秒级全文检索。

设计要点:
- FTS5 trigram 分词器：中文按 3 字子串切分，`MATCH '"电动轮椅"'` 精准子串命中，
  比 unicode61(按单字) + jieba 分词噪音小得多；2 字查询由 file_search.py 用 LIKE 兜底。
- 噪音排除: node_modules/.git/.cache/.codex_* 等 AI 工作目录与二进制/字体/资源映射
  文件默认跳过（--include-noise 可强制纳入）。
- 增量索引: path+mtime+size 不变且无 error 的文件自动跳过。
- 媒体文件仅存元数据（文件名/路径可搜），图片/视频内容识别靠 --ocr-media/--vision-*。

用法:
  python doc_indexer.py --root "D:\BaiduSyncdisk" [--db PATH] [--workers N]
  python doc_indexer.py --db PATH --stats

依赖: fitz(PyMuPDF), python-docx, openpyxl, python-pptx, xlrd, olefile, jieba(查询端用)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Lock

# ── 默认配置 ────────────────────────────────────────────────────────────
DEFAULT_ROOT = r"D:\BaiduSyncdisk"
DEFAULT_DB = r"D:\@kaifa\gongzuo-data\file_index.db"
MAX_WORKERS = 8
FRAME_CACHE_DIR = r"D:\@kaifa\gongzuo-data\frames_cache"  # 视频头尾帧缓存(OCR/描述共享)
MAX_TEXT_CHARS = 1_000_000          # 单文件正文入库上限（超限截断，防日志/大PDF撑爆库）
MAX_PDF_PAGES = 500
MAX_XLSX_CELLS = 60_000
PREVIEW_CHARS = 300
SUBPROCESS_TIMEOUT = 20

# 目录级噪音（默认排除）
EXCLUDE_DIRS = {
    "node_modules", ".git", "__pycache__", ".cache", ".venv", "venv", "env",
    "dist", "build", ".next", ".turbo", ".trash", ".thumbnails", ".trashes",
    "$recycle.bin", "system volume information", ".idea", ".vscode", ".codex",
    ".codex_tmp", ".codex_work", ".codex_runtimes", ".hermes", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".hg", ".svn", "egg-info", ".nuxt", ".svelte-kit",
}

# 扩展名级噪音（二进制/字体/缓存，默认不索引内容，仍可被文件名搜到? —— 不,完全跳过）
SKIP_EXTS = {
    ".map", ".ttf", ".otf", ".ttc", ".bcmap", ".woff", ".woff2", ".eot",
    ".exe", ".dll", ".so", ".dylib", ".wasm", ".pack", ".pyc", ".pyo",
    ".class", ".o", ".a", ".lib", ".idx", ".lock", ".whl", ".iso", ".msi",
    ".deb", ".rpm", ".pkg", ".appimage", ".icns", ".ico", ".cur", ".ani",
    ".lnk", ".tmp", ".swp", ".swo", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".bmp", ".tiff", ".tif", ".heic", ".heif", ".nef", ".cr2", ".arw", ".orf",
    ".rw2", ".dng", ".psd", ".eps", ".ai", ".mp4", ".avi", ".mov", ".mkv",
    ".wmv", ".flv", ".webm", ".m4v", ".mts", ".m2ts", ".mp3", ".wav", ".flac",
    ".m4a", ".aac", ".ogg", ".wma", ".ape", ".zip", ".7z", ".rar", ".tar",
    ".gz", ".tgz", ".bz2", ".xz",
}

# 媒体扩展名 → 元数据收录（可被文件名/路径搜索到；内容识别靠 OCR/视觉描述 pass）
MEDIA_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif",
    ".heic", ".heif", ".nef", ".cr2", ".arw", ".orf", ".rw2", ".dng",
    ".psd", ".eps", ".ai", ".svg", ".mp4", ".avi", ".mov", ".mkv", ".wmv",
    ".flv", ".webm", ".m4v", ".mts", ".m2ts", ".mp3", ".wav", ".flac",
    ".m4a", ".aac", ".ogg", ".wma", ".ape", ".3gp",
}

# 文件名级噪音（不索引、不计失败）
SKIP_NAMES = {
    "thumbs.db", "ehthumbs.db", "desktop.ini", ".ds_store",
    ".ds_store", "icon\r", "autorun.inf", ".gitignore.swp",
}

# ── Dabao-OCR 本地识别集成 ─────────────────────────────────────────────
# 图片 → Dabao-OCR 提取正文；PDF 无文字层（扫描件）→ 渲染页 → Dabao-OCR
# Dabao-OCR = Umi-OCR 剥离版（无 GUI、协议兼容、识别结果逐字符一致，同源 RapidOCR 引擎）。
# 2026-09-20 起本机唯一 OCR —— Umi-OCR(:1224) 已彻底删除（目录/进程/技能/配置全清）。
DABAO_PORT = 18224
DABAO_HOST = "127.0.0.1"
DABAO_PROJECT = r"D:\@kaifa\Dabao-OCR"
DABAO_EXE_CANDIDATES = [
    os.path.join(DABAO_PROJECT, ".venv", "Scripts", "dabao-ocr.exe"),
]
DABAO_SERVE_LOG = r"D:\@kaifa\gongzuo-data\dabao_serve.log"
# 语言提示词（取 OCR 服务 /api/ocr/get_options 的 ocr.language 枚举值）
OCR_LANG_HINTS = {
    "ch": ["简体中文", "models/config_chinese.txt", "chinese"],
    "en": ["English", "models/config_en.txt", "english"],
    "cht": ["繁體中文", "models/config_chinese_cht(v2).txt"],
}
# Dabao-OCR 直接支持的图片扩展名
OCR_IMG_EXTS = {".jpg", ".jpe", ".jpeg", ".jfif", ".png", ".webp", ".bmp", ".tif", ".tiff"}
# 可用 Pillow 转 PNG 后交给 Dabao-OCR 的扩展名（HEIC/PSD 等）
PIL_CONVERT_EXTS = {".heic", ".heif", ".psd", ".gif"}
OCR_EXTS = OCR_IMG_EXTS | PIL_CONVERT_EXTS
OCR_PDF_PAGES = 30          # 扫描件 PDF 最多 OCR 页数
OCR_MAX_IMG_SIDE = 2880     # 超过此边长先等比缩小（Dabao-OCR 提速）
OCR_HEIC_MAX_SIDE = 1280    # HEIC 手机原图缩到 720p 级再识别（上传体积-90%，提速5倍+）
OCR_MAX_FILE_MB = 30        # 超过此大小的图片跳过 OCR（产品原图/PSD 照片，避免卡死）
OCR_NO_TEXT_MARK = "【无文字图片】"
# ── Qwen3-VL 本地视觉描述集成 ─────────────────────────────────────────
# Dabao-OCR 只出文字；Qwen3-VL 补"画面里是什么"（主体/颜色/场景）→ 可搜"红色电动轮椅"
VISION_HOST = "127.0.0.1"
VISION_PORT = 1234
VISION_MAX_SIDE = 720        # 缩到 720px 提速（~17s/张 vs 原图 ~110s）
VISION_MAX_FILE_MB = 60      # 超大图跳过（视觉会缩到720px,上限放宽到60MB）
VISION_PROMPT = "用一句中文描述这张图片：主体是什么、什么颜色、什么场景。"
VISION_MARK = "【画面】"
VISION_TIMEOUT = 300
# ── 视频抽帧 OCR ───────────────────────────────────────────────────────
FFMPEG_BIN = r"C:\Users\xxx13\ffmpeg\ffmpeg-8.1.1-essentials_build\bin\ffmpeg.exe"
FFPROBE_BIN = r"C:\Users\xxx13\ffmpeg\ffmpeg-8.1.1-essentials_build\bin\ffprobe.exe"
VIDEO_FRAME_INTERVAL = 2.0  # 每 N 秒抽 1 帧
VIDEO_OCR_MAX_FRAMES = 600  # 单视频最多 OCR 帧数（防超长视频拖死）
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".mts", ".ts", ".m4v", ".avi", ".wmv", ".flv", ".3gp", ".mpg", ".mpeg"}


# 纯文本类扩展名（编码探测后直接入库）
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".json", ".yaml", ".yml", ".ini", ".cfg",
    ".conf", ".csv", ".tsv", ".log", ".srt", ".ass", ".lrc", ".xml", ".html",
    ".htm", ".css", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".py",
    ".pyw", ".sh", ".bash", ".bat", ".cmd", ".ps1", ".sql", ".java", ".c",
    ".h", ".cpp", ".hpp", ".cc", ".go", ".rs", ".rb", ".php", ".lua",
    ".swift", ".kt", ".kts", ".scala", ".groovy", ".r", ".dart", ".vue",
    ".toml", ".env", ".properties", ".reg", ".tex", ".bib", ".nfo", ".inf",
    ".url", ".gitignore", ".gitattributes", ".editorconfig", ".dockerignore",
    ".desktop", ".service", ".patch", ".diff", ".sqlite", ".db", ".plist",
    ".rst", ".adoc", ".org", ".text",
}

# 结构化文档 → 专用提取器
DOC_EXTRACTORS = {
    ".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx",
    ".doc": "doc", ".xls": "xls", ".pdf": "pdf",
    ".epub": "epub", ".xmind": "xmind", ".eml": "eml",
    ".mht": "html", ".mhtml": "html", ".odt": "odt", ".ods": "ods",
    ".odp": "odp", ".ipynb": "ipynb", ".rtf": "rtf", ".msg": "msg",
}

BINARY_ERR = "PERMANENT|binary"
TRUNC_MARK = "\n…[内容超长已截断]…"

# ── 日志 ────────────────────────────────────────────────────────────────
def setup_logging(log_path: str) -> logging.Logger:
    from logging.handlers import RotatingFileHandler
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            RotatingFileHandler(log_path, encoding="utf-8",
                                maxBytes=5 * 1024 * 1024, backupCount=3),  # 5MB×3轮转
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("doc_indexer")


# ── 数据库 ──────────────────────────────────────────────────────────────
def init_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")  # 64MB
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT UNIQUE NOT NULL,
            file_name TEXT NOT NULL,
            file_ext TEXT NOT NULL,
            file_size INTEGER,
            file_mtime TEXT,
            folder_path TEXT,
            is_media INTEGER DEFAULT 0,
            doc_type TEXT,
            content_preview TEXT,
            content_text TEXT,
            indexed_at TEXT NOT NULL,
            error TEXT
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS files_search USING fts5(
            file_path, file_name, folder_path, content_preview, content_text,
            tokenize='trigram',
            content=files, content_rowid=id
        )
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
            INSERT INTO files_search(rowid, file_path, file_name, folder_path, content_preview, content_text)
            VALUES (new.id, new.file_path, new.file_name, new.folder_path, new.content_preview, new.content_text);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
            INSERT INTO files_search(files_search, rowid, file_path, file_name, folder_path, content_preview, content_text)
            VALUES ('delete', old.id, old.file_path, old.file_name, old.folder_path, old.content_preview, old.content_text);
        END
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS files_au AFTER UPDATE OF content_text, content_preview, file_name, folder_path ON files BEGIN
            INSERT INTO files_search(files_search, rowid, file_path, file_name, folder_path, content_preview, content_text)
            VALUES ('delete', old.id, old.file_path, old.file_name, old.folder_path, old.content_preview, old.content_text);
            INSERT INTO files_search(rowid, file_path, file_name, folder_path, content_preview, content_text)
            VALUES (new.id, new.file_path, new.file_name, new.folder_path, new.content_preview, new.content_text);
        END
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_ext ON files(file_ext)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_media ON files(is_media)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder_path)")
    conn.commit()
    return conn


def is_already_indexed(conn, file_path: str, mtime: str, size: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM files WHERE file_path=? AND file_mtime=? AND file_size=? AND error IS NULL",
        (file_path, mtime, size),
    ).fetchone()
    return row is not None


# ── 文本提取 ────────────────────────────────────────────────────────────
def decode_text(data: bytes) -> str | None:
    """BOM 优先，然后 utf-8 → gbk → big5 → latin-1 兜底；二进制返回 None。"""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="ignore")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="ignore")
    for enc in ("utf-8", "gbk", "big5"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    # 二进制检测：控制字符占比过高视为二进制
    sample = data[:8192]
    ctrl = sum(1 for b in sample if b < 9 or (13 < b < 32))
    if sample and ctrl / len(sample) > 0.10:
        return None
    return data.decode("utf-8", errors="ignore")


def _cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


# ── Dabao-OCR 客户端 ───────────────────────────────────────────────────
def _dabao_is_up(timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((DABAO_HOST, DABAO_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _dabao_find_exe() -> str | None:
    for p in DABAO_EXE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def dabao_ensure_running(max_wait: int = 90) -> bool:
    """确保 Dabao-OCR(:18224) 在运行，未运行则**脱离**拉起 `dabao-ocr serve`。

    与 Dabao-OCR 时代的关键差别：Dabao-OCR 无 GUI、进程内复用引擎、启动 <2s
    （Umi 冷启动实测最长 ~2.5 分钟，所以那时才需要 200s 的等待窗口）。90s 已是宽松上限。
    """
    if _dabao_is_up():
        return True
    exe = _dabao_find_exe()
    if not exe:
        return False
    try:
        os.makedirs(os.path.dirname(DABAO_SERVE_LOG), exist_ok=True)
        log = open(DABAO_SERVE_LOG, "ab")     # 句柄由子进程继承，失败也可排查
        # 留一行「谁在什么时候拉的」：Dabao-OCR 本体启动几乎不输出，空日志无法归因
        log.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] doc_indexer: Dabao-OCR 离线 → "
                  f"拉起 serve --port {DABAO_PORT}\n".encode())
        log.flush()
        # 三旗齐加：只给 DETACHED 或只给 NO_WINDOW 时，父进程（cron/gateway）退出会把
        # 服务一起带走（2026-09-17 实盘踩过，详见 AutoETF scripts/ensure_dabao.py）。
        flags = (0x00000008 | 0x08000000 | 0x00000200) if os.name == "nt" else 0
        subprocess.Popen(
            [exe, "serve", "--port", str(DABAO_PORT)],
            cwd=DABAO_PROJECT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=flags,
        )
    except Exception:
        return False
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if _dabao_is_up():
            return True
        time.sleep(1)
    return False


def _dabao_fetch_options() -> dict:
    try:
        resp = urllib.request.urlopen(
            f"http://{DABAO_HOST}:{DABAO_PORT}/api/ocr/get_options", timeout=10
        )
        return json.loads(resp.read().decode())
    except Exception:
        return {}


def dabao_build_options(lang: str = "ch", parser: str = "single_para", limit_side: int = OCR_MAX_IMG_SIDE) -> dict:
    """动态构造 options，兼容 Rapid/Paddle 两种引擎。

    parser 默认 single_para（2026-09-11 实测切换）：截图/表格/界面类图片
    只有 single_para 能按横向阅读顺序输出（同一合成表格图 CER 31.3%→1.9%），
    multi_para 会把每一列当成独立段落，导致表格内容纵向错位；
    多段落文档场景两者均为 CER 0.0%，故统一用 single_para 无损失。
    """
    opts_def = _dabao_fetch_options()
    options = {"data.format": "text", "tbpu.parser": parser}
    if "ocr.language" in opts_def:
        allowed = opts_def["ocr.language"].get("optionsList", [])
        allowed_vals = {str(v[0]) for v in allowed}
        for hint in OCR_LANG_HINTS.get(lang, []):
            if hint in allowed_vals:
                options["ocr.language"] = hint
                break
    if "ocr.maxSideLen" in opts_def:
        options["ocr.maxSideLen"] = limit_side
    elif "ocr.limit_side_len" in opts_def:
        options["ocr.limit_side_len"] = limit_side
    if "ocr.angle" in opts_def:
        options["ocr.angle"] = True
    elif "ocr.cls" in opts_def:
        options["ocr.cls"] = True
    return options


def dabao_ocr_image(path: str, options: dict) -> str:
    """识别一张图，返回纯文本。异常抛出。"""
    b64 = _image_to_b64(path)
    payload = json.dumps({"base64": b64, "options": options}).encode()
    req = urllib.request.Request(
        f"http://{DABAO_HOST}:{DABAO_PORT}/api/ocr",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=180)
    r = json.loads(resp.read().decode())
    if r.get("code") == 100:
        return (r.get("data") or "").strip()
    if r.get("code") == 101:
        return ""  # 无文本
    raise RuntimeError(f"dabao-ocr code={r.get('code')} {str(r.get('data'))[:200]}")


def _image_to_b64(path: str) -> str:
    """读取图片为 base64。PIL_CONVERT_EXTS（HEIC/PSD）用 Pillow 转 PNG；超大图缩小。"""
    ext = os.path.splitext(path)[1].lower()
    # 伪装检测：扩展名 .jpg 但实际是 HEIC（ftyp 魔数）→ 走 Pillow 转换路径
    disguised_heic = False
    if ext in OCR_IMG_EXTS:
        try:
            with open(path, "rb") as f:
                if f.read(12)[4:8] == b"ftyp":
                    disguised_heic = True
        except OSError:
            pass
    need_convert = (ext in PIL_CONVERT_EXTS) or disguised_heic or _img_side_too_big(path)
    if need_convert:
        from PIL import Image
        if ext in (".heic", ".heif") or disguised_heic:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        with Image.open(path) as im:
            im.load()
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGB")
            w, h = im.size
            # HEIC 手机原图 → 720p 级（最长边 1280）加速；其他超限图 → 2880
            target = OCR_HEIC_MAX_SIDE if (ext in (".heic", ".heif") or disguised_heic) else OCR_MAX_IMG_SIDE
            if max(w, h) > target:
                scale = target / max(w, h)
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode()
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _img_side_too_big(path: str) -> bool:
    """边长超 OCR_MAX_IMG_SIDE 返回 True。读取失败视为 False（交给 Dabao-OCR 自己处理）。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
        return max(w, h) > OCR_MAX_IMG_SIDE
    except Exception:
        return False


def dabao_ocr_pdf(path: str, options: dict, max_pages: int = OCR_PDF_PAGES) -> str:
    """扫描件 PDF：逐页渲染 PNG → Dabao-OCR，拼接文本。返回 (文本, ocr页数)。"""
    import fitz
    doc = fitz.open(path)
    parts = []
    pages_done = 0
    try:
        for i, page in enumerate(doc):
            if i >= max_pages:
                parts.append("…[页数超限已截断]…")
                break
            pix = page.get_pixmap(dpi=200)
            buf = io.BytesIO(pix.tobytes("png"))
            b64 = base64.b64encode(buf.getvalue()).decode()
            payload = json.dumps({"base64": b64, "options": options}).encode()
            req = urllib.request.Request(
                f"http://{DABAO_HOST}:{DABAO_PORT}/api/ocr",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            resp = urllib.request.urlopen(req, timeout=180)
            r = json.loads(resp.read().decode())
            if r.get("code") == 100:
                t = (r.get("data") or "").strip()
                if t:
                    parts.append(f"[第{i+1}页]\n{t}")
            pages_done += 1
    finally:
        doc.close()
    return "\n\n".join(parts), pages_done


def extract_text(path: str, ext: str, logger: logging.Logger) -> tuple[str | None, str | None]:
    """返回 (正文, doc_type)。异常返回 (None, error)。"""
    try:
        if ext in TEXT_EXTS or ext in (".db", ".sqlite"):
            if ext in (".db", ".sqlite"):
                return None, BINARY_ERR  # sqlite 库本身不索引
            with open(path, "rb") as f:
                data = f.read(MAX_TEXT_CHARS + 8192)
            if data[:4] == b"PK\x03\x04":
                # 伪装成 txt 的 zip（改名文档/压缩包）→ 按包内清单索引
                import io
                try:
                    with zipfile.ZipFile(io.BytesIO(data)) as z:
                        return "存档内容: " + "\n".join(z.namelist()), "zip"
                except Exception:
                    pass
            text = decode_text(data)
            if text is None:
                return None, BINARY_ERR
            if len(text) > MAX_TEXT_CHARS:
                text = text[:MAX_TEXT_CHARS] + TRUNC_MARK
            return text, "text"

        kind = DOC_EXTRACTORS.get(ext)
        if kind == "docx":
            import docx
            d = docx.Document(path)
            parts = [p.text for p in d.paragraphs if p.text.strip()]
            for tbl in d.tables:
                for row in tbl.rows:
                    parts.append(" | ".join(_cell_text(c.text) for c in row.cells))
            return "\n".join(parts), "docx"

        if kind == "xlsx":
            try:
                import openpyxl
                wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
                parts, cells = [], 0
                for ws in wb.worksheets:
                    for row in ws.iter_rows(values_only=True):
                        vals = [_cell_text(v) for v in row if v is not None]
                        if vals:
                            parts.append(" ".join(vals))
                            cells += len(vals)
                        if cells > MAX_XLSX_CELLS:
                            parts.append(TRUNC_MARK)
                            break
                    if cells > MAX_XLSX_CELLS:
                        break
                wb.close()
                return "\n".join(parts), "xlsx"
            except Exception:
                # WPS 生成的不规范 xlsx（Fill 样式类型错误）→ zipfile XML 兜底
                import html as _html
                with zipfile.ZipFile(path) as z:
                    names = z.namelist()
                    sst = []
                    if "xl/sharedStrings.xml" in names:
                        xml = z.read("xl/sharedStrings.xml").decode("utf-8", errors="ignore")
                        sst = [_html.unescape(t) for t in re.findall(r"<t[^>]*>(.*?)</t>", xml, flags=re.S)]
                    parts = []
                    for n in sorted(names):
                        if re.match(r"xl/worksheets/sheet\d+\.xml$", n):
                            xml = z.read(n).decode("utf-8", errors="ignore")
                            for row in re.findall(r"<row[^>]*>(.*?)</row>", xml, flags=re.S):
                                vals = []
                                for c in re.findall(r"<c[^>]*>(.*?)</c>", row, flags=re.S):
                                    t = re.search(r"<t[^>]*>(.*?)</t>", c, flags=re.S)
                                    v = re.search(r"<v>(.*?)</v>", c, flags=re.S)
                                    if t:
                                        vals.append(_html.unescape(t.group(1)))
                                    elif v and re.search(r'\bt="s"', c):
                                        try:
                                            vals.append(sst[int(v.group(1))])
                                        except Exception:
                                            pass
                                    elif v:
                                        vals.append(v.group(1))
                                if vals:
                                    parts.append(" ".join(vals))
                return "\n".join(parts), "xlsx"

        if kind == "xls":
            import xlrd
            wb = xlrd.open_workbook(path)
            parts = []
            for ws in wb.sheets():
                for r in range(ws.nrows):
                    vals = [str(ws.cell_value(r, c)).strip() for c in range(ws.ncols)]
                    vals = [v for v in vals if v]
                    if vals:
                        parts.append(" ".join(vals))
            return "\n".join(parts), "xls"

        if kind == "pptx":
            from pptx import Presentation
            prs = Presentation(path)
            parts = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if shape.has_text_frame:
                        for para in shape.text_frame.paragraphs:
                            t = "".join(run.text for run in para.runs).strip()
                            if t:
                                parts.append(t)
                    if shape.has_table:
                        for row in shape.table.rows:
                            parts.append(" | ".join(_cell_text(c.text) for c in row.cells))
                if slide.has_notes_slide:
                    nt = slide.notes_slide.notes_text_frame.text.strip()
                    if nt:
                        parts.append(nt)
            return "\n".join(parts), "pptx"

        if kind == "doc":
            return extract_doc(path, logger), "doc"

        if kind == "pdf":
            import fitz  # PyMuPDF
            doc = fitz.open(path)
            parts = []
            has_text_layer = False
            for i, page in enumerate(doc):
                if i >= MAX_PDF_PAGES:
                    parts.append(TRUNC_MARK)
                    break
                t = page.get_text("text").strip()
                if t:
                    has_text_layer = True
                    parts.append(t)
            doc.close()
            if not parts and not has_text_layer:
                # 纯扫描件（无文字层）→ 交给 OCR pass 处理
                return None, "pdf-scan"
            return "\n".join(parts), "pdf"

        if kind == "epub":
            with zipfile.ZipFile(path) as z:
                parts = []
                for name in z.namelist():
                    if name.lower().endswith((".xhtml", ".html", ".htm", ".xml")):
                        raw = z.read(name)
                        t = decode_text(raw) or ""
                        t = re.sub(r"<[^>]+>", " ", t)
                        t = re.sub(r"\s+", " ", t).strip()
                        if t:
                            parts.append(t)
            return "\n".join(parts), "epub"

        if kind == "xmind":
            with zipfile.ZipFile(path) as z:
                content = z.read("content.json").decode("utf-8", errors="ignore")
            titles = re.findall(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"', content)
            titles = [t.encode("utf-8").decode("unicode_escape", errors="ignore") if "\\u" in t else t for t in titles]
            return "\n".join(t for t in titles if t.strip()), "xmind"

        if kind == "eml":
            import email
            with open(path, "rb") as f:
                msg = email.message_from_bytes(f.read())
            parts = []
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        parts.append(decode_text(payload) or "")
            return "\n".join(parts), "eml"

        if kind == "html":
            with open(path, "rb") as f:
                data = f.read()
            text = decode_text(data) or ""
            text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text)
            return text, "html"

        if kind in ("odt", "ods", "odp"):
            with zipfile.ZipFile(path) as z:
                raw = z.read("content.xml")
            text = decode_text(raw) or ""
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text)
            return text, kind

        if kind == "ipynb":
            import json as _json
            with open(path, "rb") as f:
                nb = _json.loads(f.read().decode("utf-8", errors="ignore"))
            parts = []
            for cell in nb.get("cells", []):
                src = "".join(cell.get("source", []))
                if src.strip():
                    parts.append(src)
            return "\n".join(parts), "ipynb"

        if kind == "rtf":
            with open(path, "rb") as f:
                data = f.read()
            if data[:5].lstrip().lower().startswith(b"{\\rtf"):
                return rtf_to_text(data), "rtf"
            text = decode_text(data)
            return (text or ""), "rtf"

        if kind == "msg":
            try:
                import olefile
                ole = olefile.OleFileIO(path)
                body = ""
                if ole.exists("__properties_version1.0"):
                    body = str(ole.openstream("__properties_version1.0").read())
                ole.close()
                return body, "msg"
            except Exception:
                return None, "PERMANENT|msg-unsupported"

        if ext in (".zip",):
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
            return "存档内容: " + "\n".join(names), "zip"
        if ext in (".7z", ".rar"):
            try:
                r = subprocess.run(
                    ["7z", "l", "-ba", path], capture_output=True, timeout=SUBPROCESS_TIMEOUT,
                    creationflags=0x08000000,  # CREATE_NO_WINDOW 防弹窗
                )
                if r.returncode == 0:
                    return "存档内容: " + r.stdout.decode("utf-8", errors="ignore"), ext
            except Exception:
                pass
            return None, "PERMANENT|no-7z"

        # 未知扩展名：尝试当文本读
        with open(path, "rb") as f:
            data = f.read(min(os.path.getsize(path), 262144))
        text = decode_text(data)
        if text is None:
            return None, BINARY_ERR
        return text, "unknown-text"
    except Exception as e:
        msg = str(e)[:200]
        return None, f"ERR|{type(e).__name__}|{msg}"


def extract_doc(path: str, logger: logging.Logger) -> str:
    """老版 .doc: 先嗅探 RTF/HTML 伪装，再 antiword，最后 olefile 粗提取。"""
    with open(path, "rb") as f:
        head = f.read(1024)
    ls = head.lstrip()
    full = open(path, "rb").read()
    if ls[:5].lower().startswith(b"{\\rtf"):
        return rtf_to_text(full)
    if ls[:5].lower() in (b"<html", b"<!doc") or b"mime" in head[:200].lower():
        text = decode_text(full) or ""
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text)
    try:
        r = subprocess.run(
            ["antiword", "-m", "UTF-8.txt", path],
            capture_output=True, timeout=SUBPROCESS_TIMEOUT,
            creationflags=0x08000000,  # CREATE_NO_WINDOW 防弹窗
        )
        if r.returncode == 0:
            return r.stdout.decode("utf-8", errors="ignore")
    except Exception:
        pass
    # olefile 粗提取：WordDocument 流按 UTF-16LE 解码，抽取可读文本段拼接
    try:
        import olefile
        ole = olefile.OleFileIO(path)
        data = ole.openstream("WordDocument").read()
        ole.close()
        t16 = data.decode("utf-16-le", errors="ignore")
        runs = re.findall(
            r"[\u4e00-\u9fff\u0020-\u007e\u3000-\u303f\uff00-\uffef\u3040-\u30ff\uac00-\ud7af]{3,}",
            t16,
        )
        out = "\n".join(r for r in runs if not _is_garbage(r))
        if out.strip():
            return out
        # 纯 ASCII 老文档兜底
        ascii_txt = re.sub(rb"[^\x20-\x7e\r\n]", b" ", data).decode("ascii", errors="ignore")
        ascii_txt = re.sub(r"[ \t]{2,}", " ", ascii_txt)
        if len(ascii_txt.strip()) > 20:
            return ascii_txt
        raise ValueError("doc-stream-unreadable")
    except Exception as e:
        raise ValueError(f"doc-extract-failed: {e}")


def _is_garbage(text: str, limit: int = 2000) -> bool:
    """乱码检测：样本中可读字符(中日韩/ASCII可打印/常见中文标点)占比过低视为乱码。"""
    sample = text[:limit]
    if not sample:
        return True
    ok = 0
    for ch in sample:
        if ch in "\r\n\t " or "\u4e00" <= ch <= "\u9fff" or "\u0020" <= ch <= "\u007e":
            ok += 1
        elif ch in "，。、；：""''（）《》【】！？·—…％￥·":
            ok += 1
        elif "\u3040" <= ch <= "\u30ff" or "\uac00" <= ch <= "\ud7af":
            ok += 1
    return ok / len(sample) < 0.5


def rtf_to_text(data: bytes) -> str:
    s = data.decode("latin-1", errors="ignore")
    s = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\([a-zA-Z]+)(-?\d+)? ?", "", s)
    s = re.sub(r"\{|\}", "", s)
    s = s.replace("\\par", "\n").replace("\\line", "\n")
    return s


# ── 扫描与入库 ──────────────────────────────────────────────────────────
def should_skip_dir(dirname: str) -> bool:
    if dirname in EXCLUDE_DIRS:
        return True
    if dirname.startswith("."):
        return True
    return False


def iter_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not should_skip_dir(d)]
        for fn in filenames:
            yield os.path.join(dirpath, fn)


def process_one(path: str, get_conn, logger: logging.Logger) -> str:
    """处理单个文件，返回状态。get_conn() 返回线程本地连接。"""
    conn = get_conn()
    try:
        st = os.stat(path)
    except OSError as e:
        return f"skip|stat-error|{e}"
    mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    ext = os.path.splitext(path)[1].lower()
    name = os.path.basename(path)
    folder = os.path.dirname(path)
    size = st.st_size

    if name.lower() in SKIP_NAMES:
        return "skip|noise-name"

    if is_already_indexed(conn, path, mtime, size):
        return "skip|unchanged"

    is_media = ext in MEDIA_EXTS
    if ext in SKIP_EXTS and not is_media:
        return "skip|noise-ext"

    if is_media:
        content, doc_type, error = None, "media", None
    else:
        content, doc_type, error = (None, None, None)
        if ext in (".docx", ".pdf", ".doc") or ext in TEXT_EXTS or ext in DOC_EXTRACTORS:
            content, doc_type = extract_text(path, ext, logger)
            if content is None:
                error = doc_type
                doc_type = None
        elif ext in (".zip", ".7z", ".rar"):
            content, doc_type = extract_text(path, ext, logger)
        else:
            content, doc_type = None, None  # 未知/二进制 → 只留元数据

    preview = ""
    if content:
        content = content.strip()
        preview = content[:PREVIEW_CHARS]

    conn.execute(
        """INSERT OR REPLACE INTO files
           (file_path, file_name, file_ext, file_size, file_mtime, folder_path,
            is_media, doc_type, content_preview, content_text, indexed_at, error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (path, name, ext, size, mtime, folder, int(is_media), doc_type,
         preview, content, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), error),
    )
    conn.commit()
    return "indexed" if content is not None else ("media" if is_media else "meta-only")


def ocr_pass(root: str, db_path: str, logger: logging.Logger, only_pdf: bool = False) -> dict:
    """Dabao-OCR 识别 pass：给无正文的图片/扫描PDF 提取文字入库。

    断点续传：已有 content_text 的自动跳过。单线程（Dabao-OCR 后端并发差）。
    返回 {"ok": n, "no_text": n, "failed": n, "skipped": n}
    """
    result = {"ok": 0, "no_text": 0, "failed": 0, "skipped": 0}
    if not dabao_ensure_running():
        logger.warning("Dabao-OCR 未运行且无法启动，OCR pass 跳过")
        result["failed"] = -1
        return result

    options = dabao_build_options()
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")

    # 收集待识别文件：图片(库内无正文) + 扫描PDF
    ext_ph = ",".join("?" * len(OCR_EXTS))
    if only_pdf:
        rows = conn.execute(
            "SELECT file_path FROM files WHERE file_ext='.pdf' AND (content_text IS NULL OR content_text='')"
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT file_path FROM files WHERE file_ext IN ({ext_ph}) AND (content_text IS NULL OR content_text='')",
            tuple(sorted(OCR_EXTS)),
        ).fetchall()
        rows += conn.execute(
            "SELECT file_path FROM files WHERE file_ext='.pdf' AND (content_text IS NULL OR content_text='')"
        ).fetchall()
    # 去重 + 检查文件仍存在
    todo = []
    seen = set()
    for (p,) in rows:
        if p in seen:
            continue
        seen.add(p)
        if os.path.exists(p):
            todo.append(p)
    logger.info("OCR pass: %d 个待识别文件", len(todo))

    for i, path in enumerate(todo, 1):
        ext = os.path.splitext(path)[1].lower()
        # 超大图片（产品原图/PSD照片）跳过 OCR：无文字价值且可能卡死
        try:
            if os.path.getsize(path) > OCR_MAX_FILE_MB * 1024 * 1024:
                conn.execute(
                    "UPDATE files SET content_text=?, content_preview=?, doc_type=?, error=NULL, indexed_at=? WHERE file_path=?",
                    (OCR_NO_TEXT_MARK, OCR_NO_TEXT_MARK, "media-ocr-skip",
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S"), path),
                )
                conn.commit()
                result["no_text"] += 1
                continue
        except OSError:
            pass
        try:
            if ext == ".pdf":
                text, _ = dabao_ocr_pdf(path, options)
                doc_type = "pdf-ocr"
            else:
                text = dabao_ocr_image(path, options)
                doc_type = "media-ocr"
        except Exception as e:
            result["failed"] += 1
            conn.execute(
                "UPDATE files SET error=? WHERE file_path=?",
                (f"ERR|OCR|{str(e)[:150]}", path),
            )
            conn.commit()
            if result["failed"] <= 10:
                logger.warning("OCR FAIL %s: %s", path, str(e)[:150])
            continue

        if text:
            content = text
            preview = text[:PREVIEW_CHARS]
            result["ok"] += 1
        else:
            content = OCR_NO_TEXT_MARK
            preview = OCR_NO_TEXT_MARK
            doc_type = doc_type + "-notext"
            result["no_text"] += 1
        conn.execute(
            "UPDATE files SET content_text=?, content_preview=?, doc_type=?, error=NULL, indexed_at=? WHERE file_path=?",
            (content, preview, doc_type, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), path),
        )
        conn.commit()
        if i % 100 == 0 or i == len(todo):
            logger.info("OCR pass 进度 %d/%d ok=%d 无文字=%d 失败=%d",
                        i, len(todo), result["ok"], result["no_text"], result["failed"])

    conn.close()
    logger.info("OCR pass 完成: 识别=%d 无文字=%d 失败=%d 跳过=%d",
                result["ok"], result["no_text"], result["failed"], result["skipped"])
    return result


def _video_duration(path: str) -> float | None:
    """ffprobe 取视频时长(秒)。失败返回 None。"""
    try:
        r = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
            creationflags=0x08000000,  # CREATE_NO_WINDOW 防弹窗
        )
        if r.returncode == 0 and r.stdout.strip():
            return float(r.stdout.strip())
    except Exception:
        pass
    return None


def _extract_frames(path: str, interval: float, max_frames: int) -> tuple[list[str], float]:
    """抽帧：每 interval 秒 1 帧到临时目录，帧缩小到 720p 级加速 OCR。
    返回 (帧路径列表, 实际间隔)。
    帧文件名 f_0001.jpg 序号 → 时间 = (seq-1) * actual_interval。
    视频短于 interval（或抽不到帧）→ 返回空列表，调用方按"无文字"处理而非失败。"""
    import tempfile
    tmpdir = tempfile.mkdtemp(prefix="vgocr_")
    pat = os.path.join(tmpdir, "f_%04d.jpg")
    vf = f"fps=1/{interval},scale='min(1280,iw)':-2"
    try:
        r = subprocess.run(
            [FFMPEG_BIN, "-v", "error", "-i", path, "-vf", vf,
             "-q:v", "3", pat],
            capture_output=True, text=True, timeout=3600,
            creationflags=0x08000000,  # CREATE_NO_WINDOW 防弹窗
        )
        if r.returncode != 0:
            return [], interval
    except Exception:
        return [], interval
    frames = sorted(os.listdir(tmpdir))
    if not frames:
        # 视频太短抽不到帧 → 用最小间隔 0.5s 再试一次
        for f in os.listdir(tmpdir):
            try:
                os.remove(os.path.join(tmpdir, f))
            except OSError:
                pass
        try:
            r = subprocess.run(
                [FFMPEG_BIN, "-v", "error", "-i", path, "-vf",
                 "fps=2,scale='min(1280,iw)':-2", "-q:v", "3", pat],
                capture_output=True, text=True, timeout=120,
                creationflags=0x08000000,  # CREATE_NO_WINDOW 防弹窗
            )
            if r.returncode == 0:
                frames = sorted(os.listdir(tmpdir))
        except Exception:
            pass
        if not frames:
            return [], 0.5
        return [os.path.join(tmpdir, f) for f in frames], 0.5
    if len(frames) > max_frames:
        # 超长视频：拉大间隔重新抽，保证全覆盖且帧数封顶
        new_interval = interval * (len(frames) / max_frames + 1)
        for f in frames:
            try:
                os.remove(os.path.join(tmpdir, f))
            except OSError:
                pass
        r = subprocess.run(
            [FFMPEG_BIN, "-v", "error", "-i", path, "-vf",
             f"fps=1/{new_interval},scale='min(1280,iw)':-2",
             "-q:v", "3", pat],
            capture_output=True, text=True, timeout=3600,
            creationflags=0x08000000,  # CREATE_NO_WINDOW 防弹窗
        )
        if r.returncode != 0:
            return [], new_interval
        frames = sorted(os.listdir(tmpdir))
        return [os.path.join(tmpdir, f) for f in frames], new_interval
    return [os.path.join(tmpdir, f) for f in frames], interval


def _extract_head_tail_frames(path: str, dur: float | None, n_head: int = 1, n_tail: int = 1,
                              use_cache: bool = True) -> list[tuple[float, str]]:
    """只抽片头/片尾各 n 帧（默认各 1 帧：2s 处 + 尾-2s 处）→ 返回 [(时间戳, 帧路径)]。

    视频 OCR/描述用：用户要求轻量抽帧，不逐 2 秒扫全片。
    dur 为 None 或抽帧失败 → 空列表。
    use_cache=True（默认）：抽帧结果按 (mtime,size,dur) 指纹缓存到 FRAME_CACHE_DIR，
    视频 OCR 与视频描述两个 pass 共享同一批帧，避免重复抽帧（2026-08-23 优化）。
    """
    if not dur or dur <= 0:
        return []
    import tempfile

    # ---- 帧缓存：命中直接返回 ----
    if use_cache:
        try:
            st = os.stat(path)
            key = f"{st.st_mtime:.0f}_{st.st_size}_{int(dur)}_{hashlib.md5(path.encode('utf-8')).hexdigest()[:8]}"
            cache_dir = os.path.join(FRAME_CACHE_DIR, key)
            idx = os.path.join(cache_dir, "index.json")
            if os.path.isfile(idx):
                with open(idx, "r", encoding="utf-8") as f:
                    meta = json.load(f)
                frames = [(float(ts), fp) for ts, fp in meta["frames"]
                          if os.path.isfile(fp)]
                if len(frames) == len(meta["frames"]):
                    return frames
        except Exception:
            pass

    ts_list = []
    head_ts = [2.0 + i * 1.0 for i in range(n_head)]
    tail_ts = [dur - 2.0 - i * 1.0 for i in range(n_tail)]
    for t in head_ts + tail_ts:
        t = round(max(0.5, min(t, max(0.5, dur - 0.5))), 1)
        ts_list.append(t)
    ts_list = sorted(set(ts_list))  # 去重排序
    tmpdir = tempfile.mkdtemp(prefix="vghead_")
    frames = []
    for j, ts in enumerate(ts_list):
        out = os.path.join(tmpdir, f"f{j}.jpg")
        try:
            r = subprocess.run(
                [FFMPEG_BIN, "-y", "-v", "error", "-ss", str(ts),
                 "-i", path, "-frames:v", "1", "-q:v", "3",
                 "-vf", "scale='min(1280,iw)':-2", out],
                capture_output=True, text=True, timeout=120,
                creationflags=0x08000000,  # CREATE_NO_WINDOW 防弹窗
            )
            if r.returncode == 0 and os.path.isfile(out):
                frames.append((ts, out))
        except Exception:
            continue

    # ---- 写缓存（帧文件复制到缓存目录，下次复用；跨卷不能用 os.replace）----
    if use_cache and frames:
        try:
            import shutil
            cache_dir = os.path.join(FRAME_CACHE_DIR, key)
            os.makedirs(cache_dir, exist_ok=True)
            meta_frames = []
            for i, (ts, fp) in enumerate(frames):
                new_fp = os.path.join(cache_dir, f"f{i}.jpg")
                try:
                    shutil.copy2(fp, new_fp)  # copy2: 跨卷安全（C:Temp→D:cache）
                    meta_frames.append([str(ts), new_fp])
                except OSError:
                    meta_frames.append([str(ts), fp])  # 复制失败保留原路径
            with open(os.path.join(cache_dir, "index.json"), "w", encoding="utf-8") as f:
                json.dump({"frames": meta_frames}, f)
            frames = [(float(ts), fp) for ts, fp in meta_frames]  # ts 存的是 str, 读回转 float
        except Exception:
            pass  # 缓存失败不影响主流程
    return frames


def video_ocr_pass(root: str, db_path: str, logger: logging.Logger,
                   exclude_dirs: tuple[str, ...] = ()) -> dict:
    """视频抽帧 OCR：头尾各 1 帧 → Dabao-OCR 识别 → 带时间戳入库。

    结果格式（content_text）:
        [00:00-00:04] 海水养硬罗马混凝土越泡越坚固
        [04:00-04:06] 古罗马人掌握了一种失传的超级水泥
    相邻帧文本相同则合并时间段；无文字帧跳过。断点续传（doc_type=video-ocr 跳过）。
    抽帧结果按 mtime/size/时长 缓存（FRAME_CACHE_DIR），与视频描述 pass 共享。
    exclude_dirs: 排除的子目录（绝对路径前缀），其中视频不识别。
    """
    result = {"ok": 0, "no_text": 0, "failed": 0, "skipped": 0}
    if not os.path.isfile(FFMPEG_BIN) or not os.path.isfile(FFPROBE_BIN):
        logger.warning("ffmpeg/ffprobe 不可用，视频 OCR pass 跳过")
        result["failed"] = -1
        return result
    if not dabao_ensure_running():
        logger.warning("Dabao-OCR 未运行且无法启动，视频 OCR pass 跳过")
        result["failed"] = -1
        return result

    options = dabao_build_options()
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")

    ext_ph = ",".join("?" * len(VIDEO_EXTS))
    rows = conn.execute(
        f"SELECT file_path FROM files WHERE file_ext IN ({ext_ph}) "
        "AND (content_text IS NULL OR content_text='' OR doc_type NOT IN ('video-ocr','video-ocr-notext'))",
        tuple(sorted(VIDEO_EXTS)),
    ).fetchall()
    todo = [p for (p,) in rows if os.path.exists(p)]
    if exclude_dirs:
        n_before = len(todo)
        todo = [p for p in todo
                if not any(p.lower().startswith(d.lower()) for d in exclude_dirs)]
        logger.info("视频 OCR pass: 排除 %d 个（%s），剩 %d 个",
                    n_before - len(todo), "、".join(exclude_dirs), len(todo))
    logger.info("视频 OCR pass: %d 个待识别视频", len(todo))

    for i, path in enumerate(todo, 1):
        try:
            dur = _video_duration(path)
            frames = _extract_head_tail_frames(path, dur, n_head=1, n_tail=1)
            if not frames:
                # 抽不到帧（极短/损坏）→ 无文字
                content = ""
            else:
                # 逐帧 OCR（头尾各 1 帧，无相邻合并需求）
                lines = []
                for ts, fp in frames:
                    try:
                        text = dabao_ocr_image(fp, options)
                    except Exception:
                        text = ""
                    if text:
                        mm, ss = int(ts // 60), int(ts % 60)
                        lines.append(f"[{mm:02d}:{ss:02d}] {text}")
                content = "\n".join(lines)
        except Exception as e:
            result["failed"] += 1
            conn.execute(
                "UPDATE files SET error=? WHERE file_path=?",
                (f"ERR|VIDEO-OCR|{str(e)[:150]}", path),
            )
            conn.commit()
            if result["failed"] <= 10:
                logger.warning("视频 OCR FAIL %s: %s", path, str(e)[:150])
            continue
        finally:
            # 清理临时帧目录（frames 元素为 (ts, path) 元组）
            fp_dir = None
            if frames:
                fp = frames[0][1] if isinstance(frames[0], tuple) else frames[0]
                fp_dir = os.path.dirname(fp)
            if fp_dir and fp_dir.startswith(os.environ.get("TEMP", "C:\\Users\\xxx13\\AppData\\Local\\Temp")):
                import shutil
                shutil.rmtree(fp_dir, ignore_errors=True)

        if content.strip():
            doc_type = "video-ocr"
            preview = content[:PREVIEW_CHARS]
            result["ok"] += 1
        else:
            content = OCR_NO_TEXT_MARK
            preview = OCR_NO_TEXT_MARK
            doc_type = "video-ocr-notext"
            result["no_text"] += 1
        conn.execute(
            "UPDATE files SET content_text=?, content_preview=?, doc_type=?, error=NULL, indexed_at=? WHERE file_path=?",
            (content, preview, doc_type, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), path),
        )
        conn.commit()
        if i % 20 == 0 or i == len(todo):
            logger.info("视频 OCR 进度 %d/%d ok=%d 无文字=%d 失败=%d",
                        i, len(todo), result["ok"], result["no_text"], result["failed"])

    conn.close()
    logger.info("视频 OCR 完成: 识别=%d 无文字=%d 失败=%d",
                result["ok"], result["no_text"], result["failed"])
    return result


def prune_missing(root: str, db_path: str, logger: logging.Logger) -> int:
    """删除库中已不存在于文件系统的记录（文件被删/移动）。返回清理条数。"""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=30000")
    rows = conn.execute("SELECT id, file_path FROM files").fetchall()
    gone = []
    for fid, fp in rows:
        if not os.path.exists(fp):
            gone.append((fid, fp))
    n = 0
    for fid, fp in gone:
        conn.execute("DELETE FROM files WHERE id=?", (fid,))
        n += 1
    conn.commit()
    conn.close()
    if n:
        logger.info("清理已删除文件记录: %d 条", n)
    return n


def scan(root: str, db_path: str, workers: int, logger: logging.Logger):
    main_conn = init_db(db_path)
    total = skipped = indexed = media = meta = errors = 0
    lock = Lock()
    local = threading.local()
    files = list(iter_files(root))
    logger.info("共发现 %d 个文件（已排除噪音目录）", len(files))

    def get_conn():
        if not hasattr(local, "conn"):
            c = sqlite3.connect(db_path, check_same_thread=False)
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=30000")
            local.conn = c
        return local.conn

    def work(path):
        return path, process_one(path, get_conn, logger)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(work, p): p for p in files}
        for i, fut in enumerate(as_completed(futs), 1):
            path, status = fut.result()
            with lock:
                if status.startswith("indexed"):
                    indexed += 1
                elif status.startswith("media"):
                    media += 1
                elif status.startswith("meta-only"):
                    meta += 1
                elif status.startswith("skip"):
                    skipped += 1
                elif status.startswith("ERR") or "error" in status:
                    errors += 1
                    logger.warning("FAIL %s: %s", path, status)
                total += 1
                if total % 200 == 0:
                    logger.info(
                        "进度 %d/%d 已索引=%d 媒体=%d 跳过=%d 失败=%d",
                        total, len(files), indexed, media, skipped, errors,
                    )
            if "ERR" in status or "error" in status:
                get_conn().execute(
                    "UPDATE files SET error=? WHERE file_path=?",
                    (status.split("|", 1)[-1], path),
                )
                get_conn().commit()
    main_conn.commit()
    logger.info("完成: 共%d 正文索引=%d 媒体元数据=%d 其余元数据=%d 跳过=%d 失败=%d",
                total, indexed, media, meta, skipped, errors)


def stats(db_path: str):
    if not os.path.exists(db_path):
        print(f"数据库不存在: {db_path}")
        return
    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    docs = conn.execute("SELECT COUNT(*) FROM files WHERE is_media=0").fetchone()[0]
    media = conn.execute("SELECT COUNT(*) FROM files WHERE is_media=1").fetchone()[0]
    errs = conn.execute("SELECT COUNT(*) FROM files WHERE error IS NOT NULL").fetchone()[0]
    no_content = conn.execute(
        "SELECT COUNT(*) FROM files WHERE is_media=0 AND (content_text IS NULL OR content_text='')"
    ).fetchone()[0]
    print(f"总文件: {total}  文档: {docs}  媒体(元数据): {media}")
    print(f"无正文(二进制/未识别): {no_content}  失败: {errs}")
    print("\n按类型:")
    for row in conn.execute(
        "SELECT COALESCE(doc_type,'?') t, COUNT(*) c FROM files GROUP BY t ORDER BY c DESC LIMIT 15"
    ):
        print(f"  {row[0]:<14} {row[1]}")
    conn.close()


def vision_desc_pass(root: str, db_path: str, logger: logging.Logger,
                     all_images: bool = False, limit: int = 0,
                     provider: str = "local") -> dict:
    """场景描述 pass：给图片补"画面里是什么"。

    provider: "local"=Qwen3-VL(127.0.0.1:1234) / "zhipu"=智谱glm-4v-flash(免费云端)。
    默认缺口模式：只处理 Dabao-OCR 无文字/未处理/超大跳过的图片；
    all_images=True 处理全部图片。
    断点续传：doc_type 以 vision 开头即跳过（可反复重跑）。
    """
    if provider == "zhipu":
        if not (os.environ.get("ZHIPU_API_KEY") or os.environ.get("ZHIPUAI_API_KEY")):
            raise SystemExit("未设置 ZHIPU_API_KEY: 智谱 open.bigmodel.cn 免费 glm-4v-flash")
    elif not _vision_up(wait=True):
        raise SystemExit(
            "视觉服务器未启动: 先跑 Startup\\Hermes_VisionServer.cmd 或 "
            "llama-server -m D:\\@kaifa\\models\\Qwen3-VL-8B-Instruct-Q4_K_M.gguf "
            "--mmproj D:\\@kaifa\\models\\mmproj-Qwen3VL-8B-Instruct-F16.gguf "
            "--host 127.0.0.1 --port 1234 -c 4096 -t 4 -ngl 0")
    conn = init_db(db_path)
    ph = ",".join("?" * len(OCR_EXTS))
    if all_images:
        rows = conn.execute(
            f"SELECT file_path FROM files WHERE is_media=1 AND file_ext IN ({ph}) "
            "AND (doc_type IS NULL OR doc_type NOT LIKE 'vision%')",
            tuple(sorted(OCR_EXTS))).fetchall()
    else:
        rows = conn.execute(
            f"SELECT file_path FROM files WHERE is_media=1 AND file_ext IN ({ph}) "
            "AND (content_text IS NULL OR content_text='' OR content_text=? OR doc_type LIKE '%skip%') "
            "AND (doc_type IS NULL OR doc_type NOT LIKE 'vision%')",
            tuple(sorted(OCR_EXTS)) + (OCR_NO_TEXT_MARK,)).fetchall()
    todo = [p for (p,) in rows if os.path.exists(p)]
    if limit:
        todo = todo[:limit]
    logger.info("VISION pass: %d 个待描述文件 (%s)", len(todo),
                "全部" if all_images else "缺口:无文字/未处理/超大跳过")
    result = {"ok": 0, "failed": 0, "skipped_big": 0}
    t_start = time.time()
    for i, path in enumerate(todo, 1):
        try:
            if os.path.getsize(path) > VISION_MAX_FILE_MB * 1024 * 1024:
                result["skipped_big"] += 1
                continue
            if provider == "zhipu":
                desc = zhipu_describe_image(path)
            else:
                desc = vision_describe_image(path)
        except Exception as e:
            result["failed"] += 1
            conn.execute("UPDATE files SET error=? WHERE file_path=?",
                         (f"ERR|VISION|{str(e)[:150]}", path))
            conn.commit()
            if result["failed"] <= 10:
                logger.warning("VISION FAIL %s: %s", path, str(e)[:150])
            continue
        row = conn.execute("SELECT content_text, doc_type FROM files WHERE file_path=?", (path,)).fetchone()
        old_text = (row[0] or "") if row else ""
        content = (f"{VISION_MARK} {desc}"
                   if not old_text or old_text == OCR_NO_TEXT_MARK
                   else f"{old_text}\n{VISION_MARK} {desc}")
        doc_type = "media-vision-zhipu" if provider == "zhipu" else (
            "media-vision-full" if all_images else "media-vision")
        conn.execute(
            "UPDATE files SET content_text=?, content_preview=?, doc_type=?, error=NULL, indexed_at=? WHERE file_path=?",
            (content, content[:PREVIEW_CHARS], doc_type,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), path))
        conn.commit()
        result["ok"] += 1
        if i % 10 == 0:
            logger.info("VISION %d/%d avg=%.1fs/张", i, len(todo), (time.time() - t_start) / i)
    conn.close()
    return result


def _vision_up(wait: bool = False) -> bool:
    """检查视觉服务器是否就绪。wait=True 时轮询等待最多 60s（服务器冷启动约 15s）。"""
    deadline = time.time() + (60 if wait else 3)
    while True:
        try:
            with urllib.request.urlopen(
                    f"http://{VISION_HOST}:{VISION_PORT}/v1/models", timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        if time.time() >= deadline:
            return False
        time.sleep(3)


def vision_describe_image(path: str, max_tokens: int = 80) -> str:
    """Qwen3-VL 本地描述一张图片（720px，返回一句中文描述）。"""
    from PIL import Image
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()  # HEIC 必须注册，否则 cannot identify image file
    except ImportError:
        pass
    im = Image.open(path)
    if im.mode != "RGB":
        im = im.convert("RGB")
    im.thumbnail((VISION_MAX_SIDE, VISION_MAX_SIDE))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()
    body = json.dumps({
        "model": "qwen3-vl",
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text", "text": VISION_PROMPT},
        ]}],
        "max_tokens": max_tokens, "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(
        f"http://{VISION_HOST}:{VISION_PORT}/v1/chat/completions",
        data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=VISION_TIMEOUT) as r:
        out = json.loads(r.read())["choices"][0]["message"]["content"]
    return out.strip()


def zhipu_describe_image(path: str, max_tokens: int = 80) -> str:
    """智谱 glm-4v-flash 免费云端描述一张图片（720px，返回一句中文描述）。

    依赖 ZHIPU_API_KEY 环境变量(.env/setx); 速度 ~1.2-1.4s/张(比本地 Qwen3-VL 快 ~20 倍)。
    坑: 64KB base64 上限内 720px 缩图直接可用; 超限自动降 512px 重试。
    """
    api_key = os.environ.get("ZHIPU_API_KEY") or os.environ.get("ZHIPUAI_API_KEY")
    if not api_key:
        raise RuntimeError("未设置 ZHIPU_API_KEY (智谱 open.bigmodel.cn 免费 glm-4v-flash)")
    from PIL import Image
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()  # HEIC 必须注册
    except ImportError:
        pass
    im = Image.open(path)
    if im.mode != "RGB":
        im = im.convert("RGB")
    im.thumbnail((VISION_MAX_SIDE, VISION_MAX_SIDE))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()
    if len(b64) > 64 * 1024:
        im.thumbnail((512, 512))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=75)
        b64 = base64.b64encode(buf.getvalue()).decode()
    body = json.dumps({
        "model": "glm-5.3-flash",
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text", "text": VISION_PROMPT},
        ]}],
        "max_tokens": max_tokens, "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        data=body, headers={"Content-Type": "application/json",
                            "Authorization": "Bearer " + api_key})
    with urllib.request.urlopen(req, timeout=VISION_TIMEOUT) as r:
        out = json.loads(r.read())["choices"][0]["message"]["content"]
    return out.strip()


def video_vision_pass(root: str, db_path: str, logger: logging.Logger,
                      limit: int = 0, provider: str = "local") -> dict:
    """视频场景描述 pass：片头 2 帧(2s/4s) + 片尾 2 帧(尾-4s/尾-2s) → 视觉模型 → 带时间戳入库。

    provider: "local"=Qwen3-VL(127.0.0.1:1234) / "zhipu"=智谱glm-4v-flash(免费云端)。
    断点续传：doc_type 以 video-vision 开头即跳过；失败标 ERR|VISIONVID 下次重试。
    """
    if provider == "zhipu":
        if not (os.environ.get("ZHIPU_API_KEY") or os.environ.get("ZHIPUAI_API_KEY")):
            raise SystemExit("未设置 ZHIPU_API_KEY: 智谱 open.bigmodel.cn 免费 glm-4v-flash")
    elif not _vision_up(wait=True):
        raise SystemExit(
            "视觉服务器未启动: 先跑 Startup\\Hermes_VisionServer.cmd 或 "
            "llama-server -m D:\\@kaifa\\models\\Qwen3-VL-8B-Instruct-Q4_K_M.gguf "
            "--mmproj D:\\@kaifa\\models\\mmproj-Qwen3VL-8B-Instruct-F16.gguf "
            "--host 127.0.0.1 --port 1234 -c 4096 -t 4 -ngl 0")
    conn = init_db(db_path)
    ext_ph = ",".join("?" * len(VIDEO_EXTS))
    rows = conn.execute(
        f"SELECT file_path FROM files WHERE is_media=1 AND file_ext IN ({ext_ph}) "
        "AND (doc_type IS NULL OR doc_type NOT LIKE 'video-vision%')",
        tuple(sorted(VIDEO_EXTS))).fetchall()
    todo = [p for (p,) in rows if os.path.exists(p)]
    if limit:
        todo = todo[:limit]
    logger.info("VISION-VIDEO pass: %d 个待识别视频 (片头2帧+片尾2帧)", len(todo))
    result = {"ok": 0, "failed": 0, "frames": 0}
    t_start = time.time()
    tmp_root = os.environ.get("TEMP", r"C:\Users\xxx13\AppData\Local\Temp")
    for i, path in enumerate(todo, 1):
        frames = []
        try:
            # 1. 时长（ffprobe）
            dur_s = subprocess.run(
                [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=60,
                creationflags=0x08000000,  # CREATE_NO_WINDOW 防弹窗
            ).stdout.strip()
            dur = float(dur_s or 0)
            # 2. 头尾各 1 帧（2s + 尾-2s），夹紧去重
            frames = _extract_head_tail_frames(path, dur, n_head=1, n_tail=1)
            # 4. 逐帧识别
            lines = []
            for ts, fpath in frames:
                if provider == "zhipu":
                    desc = zhipu_describe_image(fpath)
                else:
                    desc = vision_describe_image(fpath)
                mm, ss = int(ts // 60), int(ts % 60)
                lines.append(f"[{mm:02d}:{ss:02d}] {VISION_MARK} {desc}")
                result["frames"] += 1
            content_new = "\n".join(lines)
            row = conn.execute("SELECT content_text FROM files WHERE file_path=?", (path,)).fetchone()
            old_text = (row[0] or "") if row else ""
            # 幂等：剔除旧【画面】行再追加（重跑不重复叠加）
            kept = "\n".join(l for l in old_text.splitlines() if VISION_MARK not in l).strip()
            content = (content_new if not kept
                       else f"{kept}\n{content_new}")
            conn.execute(
                "UPDATE files SET content_text=?, content_preview=?, doc_type=?, error=NULL, indexed_at=? WHERE file_path=?",
                (content, content[:PREVIEW_CHARS], "video-vision",
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), path))
            conn.commit()
            result["ok"] += 1
        except Exception as e:
            result["failed"] += 1
            conn.execute("UPDATE files SET error=? WHERE file_path=?",
                         (f"ERR|VISIONVID|{str(e)[:150]}", path))
            conn.commit()
            if result["failed"] <= 10:
                logger.warning("视频VISION FAIL %s: %s", path, str(e)[:150])
        finally:
            if frames:
                fp_dir = os.path.dirname(frames[0][1])
                if fp_dir and fp_dir.startswith(tmp_root):
                    import shutil
                    shutil.rmtree(fp_dir, ignore_errors=True)
        if i % 5 == 0:
            logger.info("VISION-VIDEO %d/%d avg=%.1fs/条", i, len(todo),
                        (time.time() - t_start) / i)
    conn.close()
    return result


def main():
    ap = argparse.ArgumentParser(description="gongzuo 全文件索引器")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--workers", type=int, default=MAX_WORKERS)
    ap.add_argument("--ocr-media", action="store_true",
                    help="扫描后用 Dabao-OCR 识别无正文图片(断点续传,可重跑)")
    ap.add_argument("--ocr-pdf", action="store_true",
                    help="仅用 Dabao-OCR 识别扫描件PDF(无文字层)")
    ap.add_argument("--ocr-video", action="store_true",
                    help="视频抽帧OCR: 头尾各1帧→Dabao-OCR→带时间戳入库(断点续传)")
    ap.add_argument("--vision-desc", action="store_true",
                    help="Qwen3-VL 场景描述: 补齐无文字/未处理/超大跳过图片(断点续传)")
    ap.add_argument("--vision-desc-zhipu", action="store_true",
                    help="智谱glm-4v-flash 免费云端场景描述: 补齐无文字/未处理/超大跳过图片(断点续传)")
    ap.add_argument("--vision-desc-all", action="store_true",
                    help="Qwen3-VL 场景描述: 全部图片")
    ap.add_argument("--vision-video", action="store_true",
                    help="视频场景描述: 片头片尾各2帧→Qwen3-VL→带时间戳入库(断点续传)")
    ap.add_argument("--vision-video-zhipu", action="store_true",
                    help="视频场景描述: 片头片尾各2帧→智谱glm-4v-flash→带时间戳入库(断点续传)")
    ap.add_argument("--vision-limit", type=int, default=0,
                    help="VISION 限量处理(测试用)")
    ap.add_argument("--exclude-video-dir", action="append", default=[],
                    help="视频OCR排除的目录(绝对路径,可多次)。例: --exclude-video-dir \"D:\\BaiduSyncdisk\\1 蓓蓓的文件\"")
    ap.add_argument("--prune", action="store_true",
                    help="清理库中已不存在于文件系统的记录(文件被删/移动)")
    ap.add_argument("--stats", action="store_true", help="只显示库状态")
    args = ap.parse_args()

    if args.stats:
        stats(args.db)
        return

    log_path = os.path.join(os.path.dirname(args.db), "doc_indexer.log")
    logger = setup_logging(log_path)
    logger.info("开始索引 root=%s db=%s workers=%d", args.root, args.db, args.workers)
    t0 = datetime.now()
    if args.prune:
        n = prune_missing(args.root, args.db, logger)
        logger.info("PRUNE 耗时 %s -> 清理 %d 条", datetime.now() - t0, n)
        return
    if args.ocr_pdf:
        r = ocr_pass(args.root, args.db, logger, only_pdf=True)
        logger.info("OCR-PDF 耗时 %s -> %s", datetime.now() - t0, r)
        return
    if args.ocr_video:
        r = video_ocr_pass(args.root, args.db, logger,
                           exclude_dirs=tuple(args.exclude_video_dir))
        logger.info("视频OCR 耗时 %s -> %s", datetime.now() - t0, r)
        return
    if args.vision_desc or args.vision_desc_all:
        r = vision_desc_pass(args.root, args.db, logger,
                             all_images=args.vision_desc_all, limit=args.vision_limit)
        logger.info("VISION 耗时 %s -> %s", datetime.now() - t0, r)
        return
    if args.vision_desc_zhipu:
        r = vision_desc_pass(args.root, args.db, logger,
                             all_images=False, limit=args.vision_limit, provider="zhipu")
        logger.info("VISION-ZHIPU 耗时 %s -> %s", datetime.now() - t0, r)
        return
    if args.vision_video:
        r = video_vision_pass(args.root, args.db, logger, limit=args.vision_limit)
        logger.info("视频VISION 耗时 %s -> %s", datetime.now() - t0, r)
        return
    if args.vision_video_zhipu:
        r = video_vision_pass(args.root, args.db, logger, limit=args.vision_limit,
                              provider="zhipu")
        logger.info("视频VISION-ZHIPU 耗时 %s -> %s", datetime.now() - t0, r)
        return
    scan(args.root, args.db, args.workers, logger)
    if args.ocr_media:
        r = ocr_pass(args.root, args.db, logger)
        logger.info("OCR 耗时 %s -> %s", datetime.now() - t0, r)
    logger.info("耗时 %s", datetime.now() - t0)


if __name__ == "__main__":
    main()
