#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lib_to_viking.py — 任意 gongzuo 索引库 → OpenViking 增量导出（通用版）
============================================================
替代 bdsync_to_viking.py / xhp_to_viking.py（2026-08-23 合并，参数化）。

用法:
  python lib_to_viking.py --db D:\\@kaifa\\gongzuo-data\\file_index.db \
      --root D:\\BaiduSyncdisk \
      --base viking://user/default/resources/bdsync \
      --name BaiduSyncdisk

- 全量首次: 库中所有文件全部导出
- 增量: 以 (file_mtime, file_size) 为指纹, 只处理新增/变更文件
- 输出: 每文件一个 markdown → temp_upload + POST /resources (同 URI 重复添加 = 更新, 409 重试)
- 状态: <name>_to_viking.state.json（脚本同目录）
- cron 用法: no_agent=True, 有新增打印摘要, 无新增静默(exit 0)
"""
import argparse
import sqlite3
import json
import os
import sys
import time
import hashlib
import datetime
import urllib.request
import urllib.error

VIKING = "http://127.0.0.1:1933"
MAX_CHARS = 300_000          # 单文件正文入库上限
SLEEP_BETWEEN_ADDS = 0.25    # 提交间隔(秒)
CONFLICT_RETRIES = 3         # 409 busy 重试次数(队列慢时快速跳过, 留待下次增量补)
CONFLICT_BACKOFF = 4         # 秒


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(*a):
    print(now(), *a, flush=True)


def health_ok():
    try:
        r = urllib.request.urlopen(VIKING + "/health", timeout=5)
        return json.loads(r.read()).get("healthy") is True
    except Exception:
        return False


def api_post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req, timeout=60)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def upload_file(path, fname=None):
    """multipart temp_upload → temp_file_id。fname 指定资源文件名（默认取 basename）。"""
    boundary = "----lv" + hashlib.md5(path.encode() + str(time.time()).encode()).hexdigest()
    with open(path, "rb") as f:
        data = f.read()
    fname = fname or os.path.basename(path)
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{fname}"\r\n'
            f'Content-Type: text/markdown\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
    req = urllib.request.Request(VIKING + "/api/v1/resources/temp_upload", data=body,
                                 headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    r = urllib.request.urlopen(req, timeout=120)
    return json.loads(r.read())["result"]["temp_file_id"]


def add_resource(temp_file_id, uri, reason):
    """添加/更新资源; 409(busy) 重试"""
    for attempt in range(CONFLICT_RETRIES):
        code, resp = api_post(VIKING + "/api/v1/resources",
                              {"temp_file_id": temp_file_id, "to": uri, "reason": reason})
        if code == 200:
            return True
        if code == 409:
            time.sleep(CONFLICT_BACKOFF)
            continue
        log(f"  !! add 失败 HTTP {code}: {str(resp)[:200]}")
        return False
    log(f"  !! add 重试耗尽: {uri}")
    return False


def render_md(row, rel_path, lib_name):
    """把库中一条记录渲染成 markdown 文档"""
    path, name, ext, size, mtime, folder, is_media, doc_type, content, error = (
        row["file_path"], row["file_name"], row["file_ext"], row["file_size"],
        row["file_mtime"], row["folder_path"], row["is_media"], row["doc_type"],
        row["content_text"], row["error"])
    lines = [f"# {name}", ""]
    lines.append(f"- 文件: `{path}`")
    lines.append(f"- 相对路径: `{rel_path}`")
    lines.append(f"- 类型: {ext or '?'} | 大小: {size or 0} 字节 | 修改: {mtime or '?'}")
    lines.append(f"- 目录: `{folder or ''}`")
    lines.append(f"- 媒体: {'是' if is_media else '否'} | 索引类型: {doc_type or 'plain'}")
    if error:
        lines.append(f"- 索引错误: {error}")
    lines += ["", "---", ""]
    if content:
        body = str(content)
        if len(body) > MAX_CHARS:
            body = body[:MAX_CHARS] + f"\n\n…[内容过长已截断, 原长{len(body)}字符]"
        lines.append(body)
    else:
        lines.append("（无正文内容 — 纯媒体文件或未识别出文字）")
    lines += ["", "---", f"_来源: {lib_name} 自动索引导出 {now()}_"]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="gongzuo 索引库 → OpenViking 增量导出")
    ap.add_argument("--db", required=True, help="gongzuo SQLite 索引库路径")
    ap.add_argument("--root", required=True, help="被索引的根目录(用于算相对路径)")
    ap.add_argument("--base", required=True, help="viking 资源基础 URI, 如 viking://user/default/resources/bdsync")
    ap.add_argument("--name", required=True, help="库显示名(用于状态文件与摘要)")
    ap.add_argument("--state-name", default=None,
                    help="状态文件基名(默认=--name; 旧脚本迁移时传 xhp/bdsync 对齐旧 state)")
    ap.add_argument("--reason", default="gongzuo 自动索引导出", help="viking 资源 reason")
    ap.add_argument("--max-adds", type=int, default=0,
                    help="本轮最多成功提交新文件数(0=不限); 分批导出用, 防止 viking 队列积压导致后半段全 409")
    args = ap.parse_args()
    if not args.state_name:
        args.state_name = args.name

    state_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              f"{args.state_name}_to_viking.state.json")
    lock_file = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             f"{args.name}_to_viking.lock")

    # 防重入锁
    if os.path.exists(lock_file):
        age = time.time() - os.path.getmtime(lock_file)
        if age < 7200:
            print(f"[{now()}] 已有导出实例在运行 (锁文件 {age:.0f} 秒前更新), 跳过")
            return 0
        os.remove(lock_file)
    open(lock_file, "w").close()
    try:
        return _main(args, state_file)
    finally:
        try:
            os.remove(lock_file)
        except OSError:
            pass


def _main(args, state_file):
    if not health_ok():
        print(f"[{now()}] OpenViking 不可达 ({VIKING}), 跳过本次导出")
        return 1
    if not os.path.exists(args.db):
        print(f"[{now()}] 索引库不存在: {args.db} (先跑 doc_indexer.py)")
        return 1

    known = {}
    if os.path.exists(state_file):
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                known = json.load(f).get("files", {})
        except Exception:
            pass

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT file_path, file_name, file_ext, file_size, file_mtime, folder_path, "
        "is_media, doc_type, content_text, error FROM files"
    ).fetchall()
    con.close()

    changed = skipped = 0
    for row in rows:
        if args.max_adds and changed >= args.max_adds:
            print(f"[{now()}] 已达本轮上限 --max-adds {args.max_adds} (成功 {changed}), 剩余留待下一轮")
            break
        path = row["file_path"]
        if not os.path.exists(path):
            continue
        try:
            real_mtime = os.path.getmtime(path)
            real_size = os.path.getsize(path)
        except OSError:
            continue
        fingerprint = f"{real_mtime:.0f}|{real_size}"
        if known.get(path) == fingerprint:
            skipped += 1
            continue  # 未变化 → 跳过

        rel = os.path.relpath(path, args.root)
        if rel.startswith(".."):
            # 文件在 root 外（如主库混入的 @佳康顺 聊天记录）：
            # 用 盘符前缀 + 去前导斜杠的绝对路径 作安全 URI，避免 viking 拒绝 '..' traversal
            abs_norm = os.path.abspath(path).replace("\\", "/").lstrip("/")
            drive, _, rest = abs_norm.partition(":")
            uri_path = (drive.lower() + "_" + rest.lstrip("/")) if drive else abs_norm
        else:
            uri_path = rel.replace("\\", "/")
        base, _ = os.path.splitext(uri_path)
        uri = f"{args.base}/{base}.md"
        try:
            md = render_md(row, rel, args.name)
            fname = os.path.basename(uri)
            tmp = path + ".viking.tmp.md"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(md)
            tid = upload_file(tmp, fname=fname)
            ok = add_resource(tid, uri, args.reason)
            os.remove(tmp)
            if ok:
                known[path] = fingerprint
                changed += 1
                if changed <= 8:
                    log(f"  ✓ {rel}")
            else:
                known.pop(path, None)
        except Exception as e:
            log(f"  !! 导出失败 {rel}: {e}")
            known.pop(path, None)
        time.sleep(SLEEP_BETWEEN_ADDS)

    with open(state_file, "w", encoding="utf-8") as f:
        json.dump({"files": known}, f, ensure_ascii=False, indent=1)

    total = len(rows)
    if changed == 0:
        return 0  # 无新增 → 静默
    print(f"[{now()}] {args.name}→Viking 增量导出完成: 库 {total} 文件, 本次新增/更新 {changed}, 未变跳过 {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
