# -*- coding: utf-8 -*-
r"""
全文件秒级搜索 — gongzuo 扩展
============================
对 doc_indexer.py 建立的 SQLite FTS5(trigram) 库做中文全文检索。

用法:
  python file_search.py "电动轮椅"                     # 基础搜索
  python file_search.py "接待话术" --ext doc,docx      # 按扩展名过滤
  python file_search.py "提成" --folder "12 抖店"      # 按目录过滤
  python file_search.py "展会" --type media            # 只搜媒体(元数据+AI描述)
  python file_search.py "报表" --limit 50 --copy 3     # 复制第3个结果的路径
  python file_search.py "关键词" --open 1              # 打开第1个结果
  python file_search.py "关键词" --dir 2               # 在资源管理器中定位
  python file_search.py "关键词" --content 1           # 打印第1个结果的全文
  python file_search.py "关键词" --json                # JSON输出(供agent调用)
  python file_search.py --stats                        # 库状态

搜索策略:
- jieba 分词 → ≥3字符词组用 FTS5 trigram 精准子串匹配(MATCH '"电动轮椅"')
- <3字符(如"轮椅")用 LIKE 兜底(文件名+目录+预览+正文前2万字)
- 命中0条时自动降级为纯 LIKE 子串搜索，保证召回
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import warnings

warnings.filterwarnings("ignore", category=UserWarning)  # jieba/pkg_resources 弃用噪音

DEFAULT_DB = r"D:\@kaifa\gongzuo-data\file_index.db"
LIKE_SPAN = 20_000  # LIKE 兜底扫描的正文长度(每文件前2万字)

_HIL = "\033[31m"  # 高亮色(红)
_RST = "\033[0m"

STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "及", "或", "把", "被", "让", "给",
    "对", "从", "向", "于", "而", "则", "但", "并", "等", "就", "都", "很",
    "有", "为", "之", "以", "这", "那", "我", "你", "他", "她", "它", "们",
    "一个", "这个", "那个", "进行", "以及", "可以", "我们", "你们", "他们",
}


def tokenize(query: str):
    import jieba
    jieba.setLogLevel(60)
    toks = [t.strip() for t in jieba.cut_for_search(query) if t.strip()]
    seen, out = set(), []
    for t in toks:
        if re.fullmatch(r"[\W_]+", t):      # 纯标点
            continue
        if t in STOPWORDS or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def qq(tok: str) -> str:
    """FTS5 短语转义"""
    return '"' + tok.replace('"', '""') + '"'


def search(conn: sqlite3.Connection, query: str, ext=None, folder=None,
           doc_type=None, limit=30, name_only=False):
    toks = tokenize(query)
    if not toks:
        return [], toks

    long_toks = [t for t in toks if len(t) >= 3]
    short_toks = [t for t in toks if len(t) < 3]
    match_expr = " AND ".join(qq(t) for t in long_toks) if long_toks else None

    # 基础过滤条件（ext/folder/type）——顺序与占位符一致
    base_conds, base_params = [], []
    if ext:
        exts = [e if e.startswith(".") else "." + e for e in ext.split(",")]
        base_conds.append("m.file_ext IN (%s)" % ",".join("?" * len(exts)))
        base_params.extend(exts)
    if folder:
        base_conds.append("m.folder_path LIKE ?")
        base_params.append(f"%{folder}%")
    if doc_type == "media":
        base_conds.append("m.is_media = 1")
    elif doc_type == "doc":
        base_conds.append("m.is_media = 0")

    def like_cond(tok):
        pat = f"%{tok}%"
        if name_only:
            return "(m.file_name LIKE ? OR m.folder_path LIKE ?)", [pat, pat]
        return ("(m.file_name LIKE ? OR m.folder_path LIKE ? "
                "OR m.content_preview LIKE ? OR substr(m.content_text,1,%d) LIKE ?)" % LIKE_SPAN), \
               [pat, pat, pat, pat]

    rows, used = None, None
    # 路径1: FTS5 trigram 精准匹配
    if match_expr:
        conds = ["files_search MATCH ?", *base_conds]
        short_conds, short_params = [], []
        for tok in short_toks:
            lc, lp = like_cond(tok)
            short_conds.append(lc)
            short_params.extend(lp)
        sql = (f"SELECT m.id, m.file_path, m.file_name, m.file_ext, m.file_size, m.file_mtime, "
               f"m.folder_path, m.is_media, m.doc_type, m.content_preview, rank AS r "
               f"FROM files m JOIN files_search ON files_search.rowid = m.id "
               f"WHERE {' AND '.join(conds + short_conds)} "
               f"ORDER BY CASE WHEN m.is_media=1 THEN 0 ELSE 1 END, r LIMIT ?")
        params = [match_expr, *base_params, *short_params, limit]
        try:
            rows = conn.execute(sql, params).fetchall()
            used = "FTS5"
        except sqlite3.OperationalError:
            rows = None

    # 路径2: LIKE 兜底（短词查询 / FTS 无结果且原本就含短词时）
    if rows is None or (not rows and short_toks):
        conds2 = list(base_conds)
        params2 = list(base_params)
        for tok in toks:
            lc, lp = like_cond(tok)
            conds2.append(lc)
            params2.extend(lp)
        sql = (f"SELECT m.id, m.file_path, m.file_name, m.file_ext, m.file_size, m.file_mtime, "
               f"m.folder_path, m.is_media, m.doc_type, m.content_preview, 0 AS r "
               f"FROM files m WHERE {' AND '.join(conds2)} "
               f"ORDER BY CASE WHEN m.is_media=1 THEN 0 ELSE 1 END, "
               f"CASE WHEN m.file_name LIKE ? THEN 0 ELSE 1 END, m.file_mtime DESC LIMIT ?")
        rows = conn.execute(sql, params2 + [f"%{query}%", limit]).fetchall()
        used = "LIKE"
    return rows, toks


def fmt_size(n):
    if n is None:
        return "?"
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f}{u}" if u == "B" else f"{n:.1f}{u}"
        n /= 1024


def highlight(text: str, toks, enable: bool) -> str:
    if not enable or not toks:
        return text
    for t in sorted(toks, key=len, reverse=True):
        if not t:
            continue
        text = re.sub(re.escape(t), lambda m: f"{_HIL}{m.group(0)}{_RST}", text, flags=re.I)
    return text


def print_results(rows, toks, limit, json_out=False):
    if json_out:
        out = [{
            "id": r[0], "path": r[1], "name": r[2], "ext": r[3], "size": r[4],
            "mtime": r[5], "folder": r[6], "is_media": bool(r[7]), "doc_type": r[8],
            "preview": (r[9] or "")[:500],
        } for r in rows]
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return
    if not rows:
        print("无结果")
        return
    tty = sys.stdout.isatty()
    kind_icon = {0: "📄", 1: "🎬"}
    for i, r in enumerate(rows, 1):
        icon = kind_icon.get(r[7] or 0, "📄")
        line = f"[{i}] {icon} {r[1]}"
        if tty:
            line = highlight(line, toks, True)
        extra = []
        if r[8]:
            extra.append(r[8])
        extra.append(fmt_size(r[4]))
        if r[5]:
            extra.append(r[5])
        print(line)
        if r[9]:
            prev = re.sub(r"\s+", " ", r[9]).strip()
            print("     " + highlight(prev[:160], toks, tty))
        print(f"     ({', '.join(extra)})")
    print(f"--- {len(rows)} 条 (前 {limit} 条)")


def do_action(rows, conn, action, idx):
    if not rows or idx < 1 or idx > len(rows):
        print(f"无第 {idx} 个结果")
        return
    r = rows[idx - 1]
    path = r[1]
    if action == "copy":
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"Set-Clipboard -Value '{path}'"], check=False)
        print(f"已复制: {path}")
    elif action == "open":
        os.startfile(path)
        print(f"已打开: {path}")
    elif action == "dir":
        subprocess.run(["explorer", "/select,", path], check=False)
        print(f"已定位: {path}")
    elif action == "content":
        row = conn.execute("SELECT content_text FROM files WHERE id=?", (r[0],)).fetchone()
        print(f"==== {path} ====")
        print(row[0] if row and row[0] else "(无正文/二进制文件)")


def stats(db_path):
    if not os.path.exists(db_path):
        print(f"数据库不存在: {db_path}")
        return
    conn = sqlite3.connect(db_path)
    total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    docs = conn.execute("SELECT COUNT(*) FROM files WHERE is_media=0").fetchone()[0]
    media = conn.execute("SELECT COUNT(*) FROM files WHERE is_media=1").fetchone()[0]
    errs = conn.execute("SELECT COUNT(*) FROM files WHERE error IS NOT NULL").fetchone()[0]
    db_size = os.path.getsize(db_path) / 1024 / 1024
    print(f"文件总数: {total}  文档: {docs}  媒体元数据: {media}  失败: {errs}")
    print(f"库大小: {db_size:.1f} MB")
    conn.close()


def main():
    ap = argparse.ArgumentParser(description="gongzuo 全文件秒级搜索")
    ap.add_argument("query", nargs="?", default=None)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--ext", default=None, help="扩展名过滤, 逗号分隔: pdf,docx")
    ap.add_argument("--folder", default=None, help="目录过滤: 12 抖店")
    ap.add_argument("--type", dest="doc_type", default=None, choices=["doc", "media"])
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--name-only", action="store_true", help="只在文件名/路径中搜")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--copy", type=int, help="复制第N个结果的路径到剪贴板")
    ap.add_argument("--open", type=int, help="打开第N个结果")
    ap.add_argument("--dir", type=int, help="资源管理器定位第N个结果")
    ap.add_argument("--content", type=int, help="打印第N个结果的全文")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    if args.stats:
        stats(args.db)
        return
    if not args.query:
        ap.print_help()
        return

    conn = sqlite3.connect(args.db)
    rows, toks = search(conn, args.query, ext=args.ext, folder=args.folder,
                        doc_type=args.doc_type, limit=args.limit, name_only=args.name_only)
    print_results(rows, toks, args.limit, json_out=args.json)

    for act in ("copy", "open", "dir", "content"):
        v = getattr(args, act)
        if v:
            do_action(rows, conn, act, v)
    conn.close()


if __name__ == "__main__":
    main()
