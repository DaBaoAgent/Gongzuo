#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
【主副本说明】本文件为技能目录中的 git 管理主副本，cron 实际运行的是
hermes/scripts/hermes_to_viking.py（薄副本）。
改逻辑改本文件后必须同步副本；不要直接运行本文件（state 会写技能目录）。
"""
"""
hermes_to_viking.py — Hermes 会话(state.db) → Obsidian 增量导出
============================================================
- 全量首次导出: 所有会话全部消息(含 role=tool 的 tool 消息、tool_calls)
- 增量: 以 messages.id 为游标, 只处理新消息
- 输出: 每会话每日一个 markdown 文件 → D:\\@kaifa\\Obsidian知识库\\03 资料库\\Hermes会话完整原文\\
- 说明: OpenViking 已于 2026-11-10 归档退役，本脚本不再上传任何服务，只写磁盘
- 状态: 与脚本同目录 hermes_to_viking.state.json (last_message_id + 每文件 md5)
- cron 用法: no_agent=True, 有新增打印摘要, 无新增静默(exit 0)
"""
import sqlite3
import json
import os
import sys
import time
import hashlib
import datetime

HERMES_DB = r"C:\Users\20200\AppData\Local\hermes\state.db"
OUT_DIR = r"D:\@kaifa\Obsidian知识库\03 资料库\Hermes会话完整原文"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hermes_to_viking.state.json")
MAX_MSG_CHARS = 50000          # 单条消息内容截断上限


def now():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(*a):
    print(now(), *a, flush=True)


def fmt_ts(ts):
    if not ts or ts <= 0:
        return "?"
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def trunc(s):
    if s is None:
        return ""
    s = str(s)
    if len(s) > MAX_MSG_CHARS:
        return s[:MAX_MSG_CHARS] + f"\n…[内容过长已截断, 原长{len(s)}字符]"
    return s


def render_messages(rows, session_meta):
    """rows: (role, content, tool_name, tool_calls, timestamp, reasoning_content) 按 id 升序"""
    lines = [f"# Hermes 会话导出", ""]
    for k, v in session_meta.items():
        lines.append(f"- {k}: {v}")
    lines += ["", f"- 消息条数: {len(rows)}", "", "---", ""]
    for role, content, tool_name, tool_calls, ts, reasoning in rows:
        ts_s = fmt_ts(ts)
        if role == "user":
            lines.append(f"## 👤 用户 ({ts_s})")
        elif role == "assistant":
            lines.append(f"## 🤖 助手 ({ts_s})")
            if reasoning:
                lines += ["", f"<details><summary>推理过程</summary>", "", trunc(reasoning), "", "</details>"]
            if tool_calls:
                lines += ["", "工具调用:"] + ["```json\n" + trunc(tool_calls) + "\n```"]
        elif role == "tool":
            lines.append(f"## 🔧 工具: {tool_name} ({ts_s})")
        elif role == "session_meta":
            lines.append(f"## 📋 会话元数据 ({ts_s})")
        else:
            lines.append(f"## {role} ({ts_s})")
        if content:
            lines += ["", trunc(content), ""]
        lines.append("---")
    return "\n".join(lines)


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_message_id": 0, "files": {}}


def save_state(st):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def md5_of(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hermes_to_viking.lock")


def main():
    # 防重入锁: cron 与手动运行并发时只允许一个实例
    if os.path.exists(LOCK_FILE):
        age = time.time() - os.path.getmtime(LOCK_FILE)
        if age < 7200:  # 2小时内视为仍在运行
            print(f"[{now()}] 已有导出实例在运行 (锁文件 {age:.0f} 秒前更新), 跳过")
            return 0
        os.remove(LOCK_FILE)
    open(LOCK_FILE, "w").close()
    try:
        return _main()
    finally:
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass


def _main():
    if not os.path.exists(HERMES_DB):
        print(f"[{now()}] state.db 不存在: {HERMES_DB}")
        return 1

    st = load_state()
    cursor_id = st.get("last_message_id", 0)

    con = sqlite3.connect(f"file:{HERMES_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # 新消息
    new = con.execute(
        """SELECT m.id, m.session_id, m.role, m.content, m.tool_name, m.tool_calls,
                  m.timestamp, m.reasoning_content
           FROM messages m WHERE m.id > ? ORDER BY m.id""", (cursor_id,)).fetchall()
    if not new:
        return 0  # 无新消息 → 静默

    sess_ids = sorted({r["session_id"] for r in new})
    meta = {}
    for sid in sess_ids:
        s = con.execute("SELECT id, title, model, profile_name, started_at, ended_at, cwd, message_count "
                        "FROM sessions WHERE id=?", (sid,)).fetchone()
        if s:
            meta[sid] = {
                "会话ID": s["id"], "标题": s["title"] or "", "模型": s["model"] or "",
                "Profile": s["profile_name"] or "default",
                "开始": fmt_ts(s["started_at"]), "结束": fmt_ts(s["ended_at"]),
                "工作目录": s["cwd"] or "",
            }

    # 需要重建的 (session, 日期) 桶
    buckets = {}
    for r in new:
        day = datetime.datetime.fromtimestamp(r["timestamp"]).strftime("%Y-%m-%d") if r["timestamp"] and r["timestamp"] > 0 else "unknown"
        buckets.setdefault((r["session_id"], day), set()).add(r["id"])

    changed = 0
    for (sid, day), _ids in sorted(buckets.items()):
        day_dir = os.path.join(OUT_DIR, sid)
        os.makedirs(day_dir, exist_ok=True)
        path = os.path.join(day_dir, day + ".md")

        rows = con.execute(
            """SELECT role, content, tool_name, tool_calls, timestamp, reasoning_content
               FROM messages WHERE session_id=? AND timestamp>0
               ORDER BY id""", (sid,)).fetchall()
        # 当天文件只取当天的消息; 但首行头用会话元数据
        day_rows = [r for r in rows if datetime.datetime.fromtimestamp(r["timestamp"]).strftime("%Y-%m-%d") == day]
        if not day_rows:
            day_rows = rows  # 无时间戳兜底
        content = render_messages(day_rows, meta.get(sid, {}))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)

        old_md5 = st["files"].get(path)
        new_md5 = md5_of(tmp)
        if old_md5 == new_md5:
            os.remove(tmp)
            continue

        os.replace(tmp, path)
        st["files"][path] = new_md5
        changed += 1
        log(f"  ✓ {os.path.relpath(path, OUT_DIR)}")

    st["last_message_id"] = max(r["id"] for r in new)
    save_state(st)
    con.close()

    n = len(new)
    print(f"[{now()}] Hermes→Obsidian 增量导出完成: 新消息 {n} 条, 涉及会话 {len(sess_ids)} 个, "
          f"更新文件 {changed} 个 (游标 → {st['last_message_id']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
