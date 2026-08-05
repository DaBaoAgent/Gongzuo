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
- 媒体文件仅存元数据，--merge-media 可从既有 media_index.db 回填 AI 视觉描述，
  实现"一个库搜全部"。

用法:
  python doc_indexer.py --root "D:\BaiduSyncdisk" [--db PATH] [--workers N]
  python doc_indexer.py --root "D:\BaiduSyncdisk" --merge-media D:\zhipuai_test\media_index.db
  python doc_indexer.py --db PATH --stats

依赖: fitz(PyMuPDF), python-docx, openpyxl, python-pptx, xlrd, olefile, jieba(查询端用)
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from threading import Lock

# ── 默认配置 ────────────────────────────────────────────────────────────
DEFAULT_ROOT = r"D:\BaiduSyncdisk"
DEFAULT_DB = r"D:\zhipuai_test\file_index.db"
MAX_WORKERS = 8
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

# 媒体扩展名 → 元数据收录（可被文件名/路径搜索到；内容回填靠 --merge-media）
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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
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
            for i, page in enumerate(doc):
                if i >= MAX_PDF_PAGES:
                    parts.append(TRUNC_MARK)
                    break
                t = page.get_text("text").strip()
                if t:
                    parts.append(t)
            doc.close()
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
                    ["7z", "l", "-ba", path], capture_output=True, timeout=SUBPROCESS_TIMEOUT
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


def merge_media_descriptions(conn: sqlite3.Connection, media_db_path: str, logger: logging.Logger) -> int:
    """从既有 media_index.db 回填 AI 视觉描述到 files.content_text。"""
    if not os.path.exists(media_db_path):
        logger.warning("media db 不存在: %s", media_db_path)
        return 0
    src = sqlite3.connect(media_db_path)
    rows = src.execute(
        "SELECT file_path, description, tags FROM media_files WHERE error IS NULL AND description IS NOT NULL"
    ).fetchall()
    src.close()
    n = 0
    for path, desc, tags in rows:
        merged = ""
        if desc:
            merged += desc
        if tags:
            merged += "\n标签: " + tags
        if merged:
            merged = "【AI视觉描述】" + merged
            cur = conn.execute(
                "UPDATE files SET content_text = ?, content_preview = substr(?, 1, %d), doc_type = 'media-ai' "
                "WHERE file_path = ? AND is_media = 1" % PREVIEW_CHARS,
                (merged, merged, path),
            )
            n += cur.rowcount
    conn.commit()
    return n


def scan(root: str, db_path: str, workers: int, merge_media: str | None, logger: logging.Logger):
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
    if merge_media:
        n = merge_media_descriptions(main_conn, merge_media, logger)
        logger.info("媒体描述回填: %d 条", n)
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


def main():
    ap = argparse.ArgumentParser(description="gongzuo 全文件索引器")
    ap.add_argument("--root", default=DEFAULT_ROOT)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--workers", type=int, default=MAX_WORKERS)
    ap.add_argument("--merge-media", default=None, help="既有媒体库路径，回填AI描述")
    ap.add_argument("--stats", action="store_true", help="只显示库状态")
    args = ap.parse_args()

    if args.stats:
        stats(args.db)
        return

    log_path = os.path.join(os.path.dirname(args.db), "doc_indexer.log")
    logger = setup_logging(log_path)
    logger.info("开始索引 root=%s db=%s workers=%d", args.root, args.db, args.workers)
    t0 = datetime.now()
    scan(args.root, args.db, args.workers, args.merge_media, logger)
    logger.info("耗时 %s", datetime.now() - t0)


if __name__ == "__main__":
    main()
