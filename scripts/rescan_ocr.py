# -*- coding: utf-8 -*-
"""重扫已入库的 media-ocr 图片（重建正文文本顺序）。

背景：2026-09-11 本地 OCR 的 tbpu.parser 由 multi_para 改为 single_para，
存量记录仍是旧顺序（表格/界面截图纵向错位）。本脚本用当前配置重新 OCR 并更新库。

幂等：默认只取 doc_type='media-ocr' 且 indexed_at 早于本次启动时间的记录。
      处理成功后 indexed_at 会刷新为当前时间，因此中断后直接重跑即可续传。
并发：本地 OCR（现为 Dabao-OCR :18224）后端可承受 4 路并发（实测 4 张墙钟 2.55s，与单张串行无损耗）。
      识别在工作线程池，写库集中在主线程（SQLite 单写者）。
用法：
      python rescan_ocr.py                # 全量重扫
      python rescan_ocr.py --limit 200    # 试跑 200 张（看速与抽样效果）
      python rescan_ocr.py --workers 6
"""
import argparse
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import doc_indexer as di  # noqa: E402


def ocr_one(path, options):
    """返回 (path, text, error)。不抛异常。"""
    try:
        if os.path.getsize(path) > di.OCR_MAX_FILE_MB * 1024 * 1024:
            return (path, None, "SKIP_TOO_BIG")
        return (path, di.dabao_ocr_image(path, options), None)
    except Exception as e:  # noqa: BLE001
        return (path, None, "%s: %s" % (type(e).__name__, str(e)[:150]))


def main():
    ap = argparse.ArgumentParser(description="重扫 media-ocr 存量记录")
    ap.add_argument("--db", default=di.DEFAULT_DB)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 张（试跑用）")
    ap.add_argument("--log", default="", help="日志文件路径")
    args = ap.parse_args()

    run_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def log(msg):
        line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
        print(line, flush=True)
        if args.log:
            with open(args.log, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    if not di.dabao_ensure_running():
        log("Dabao-OCR 未运行且无法启动，退出")
        return 1

    options = di.dabao_build_options()
    log("下发 options: %s" % {k: v for k, v in options.items()})

    conn = sqlite3.connect(args.db, check_same_thread=False)
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA journal_mode=WAL")

    rows = conn.execute(
        "SELECT file_path FROM files WHERE doc_type='media-ocr' AND indexed_at < ? ORDER BY file_path",
        (run_start,),
    ).fetchall()
    todo = [p for (p,) in rows if os.path.exists(p)]
    if args.limit:
        todo = todo[: args.limit]
    log("待重扫 %d 张（库内不存在的 %d 张已跳过），workers=%d" % (len(todo), len(rows) - len(todo), args.workers))
    if not todo:
        log("没有待重扫记录（已是最新）")
        return 0

    ok = no_text = failed = skipped = 0
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(ocr_one, p, options): p for p in todo}
        for fut in as_completed(futs):
            path, text, err = fut.result()
            done += 1
            if err == "SKIP_TOO_BIG":
                skipped += 1
                continue
            if err:
                failed += 1
                conn.execute("UPDATE files SET error=? WHERE file_path=?", ("ERR|OCR|" + err, path))
            elif text:
                ok += 1
                conn.execute(
                    "UPDATE files SET content_text=?, content_preview=?, doc_type=?, error=NULL, indexed_at=? WHERE file_path=?",
                    (text, text[: di.PREVIEW_CHARS], "media-ocr", run_start, path),
                )
            else:
                no_text += 1
                conn.execute(
                    "UPDATE files SET content_text=?, content_preview=?, doc_type=?, error=NULL, indexed_at=? WHERE file_path=?",
                    (di.OCR_NO_TEXT_MARK, di.OCR_NO_TEXT_MARK, "media-ocr-notext", run_start, path),
                )
            if done % 50 == 0:
                conn.commit()
                el = time.time() - t0
                rate = done / el if el else 0
                eta = (len(todo) - done) / rate / 60 if rate else 0
                log("进度 %d/%d  识别=%d 无文字=%d 失败=%d  |  %.2f 张/秒  剩余约 %.0f 分钟"
                    % (done, len(todo), ok, no_text, failed, rate, eta))
    conn.commit()
    conn.close()
    el = time.time() - t0
    log("完成: 共 %d 张，识别 %d，无文字 %d，失败 %d，跳过 %d，耗时 %.1f 分钟 (%.2f 张/秒)"
        % (done, ok, no_text, failed, skipped, el / 60, done / el if el else 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
