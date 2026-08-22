#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CipherTalk 微信聊天导出 json → 干净聊天文本 txt
用途: gongzuo 索引前预处理。原始 json 含大量 JSON 语法噪音且大文件超 100 万字符截断,
转为 "[时间] 发送者: 内容" 一行一条消息的 txt 后, 正文压缩比高、检索质量好。

用法:
    python chat_json_to_txt.py <聊天记录根目录> [--force]

行为:
- 遍历根目录下所有 *.json (CipherTalk 格式: {exportInfo, session, messages})
- 每个 json 输出到 同目录/_chattext/<原文件名>.txt
- 幂等: 源 json mtime 未变且 txt 已存在则跳过
- 文本消息 → "[formattedTime] senderDisplayName: content"
- 非文本消息 → "[formattedTime] senderDisplayName: 【类型】content前缀"
"""
import argparse
import json
import os
import sys

TEXT_TYPES = {"文本消息", "引用消息", "转账消息", "文件消息"}  # 文件消息 content 含文件名

def convert_json(json_path: str) -> str:
    """返回转换后的聊天文本, 失败抛异常"""
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    msgs = data.get("messages") if isinstance(data, dict) else None
    if msgs is None:
        return ""  # 非 CipherTalk 格式, 跳过
    lines = []
    for m in msgs:
        if not isinstance(m, dict):
            continue
        t = m.get("formattedTime") or ""
        sender = m.get("senderDisplayName") or ("我" if m.get("isSend") else "对方")
        ctype = m.get("type") or ""
        content = (m.get("content") or "").strip()
        if not content:
            content = (m.get("rawContent") or "")[:120]
        if ctype and ctype not in TEXT_TYPES and content:
            content = f"【{ctype}】{content[:200]}"
        elif not content:
            content = f"【{ctype or '消息'}】"
        lines.append(f"[{t}] {sender}: {content}")
    return "\n".join(lines)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", help="聊天记录根目录")
    ap.add_argument("--force", action="store_true", help="强制全部重新转换")
    args = ap.parse_args()
    root = args.root
    total = converted = skipped = 0
    src_chars = out_chars = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "_chattext"]  # 不进 _chattext
        for fn in filenames:
            if not fn.lower().endswith(".json"):
                continue
            src = os.path.join(dirpath, fn)
            out_dir = os.path.join(dirpath, "_chattext")
            os.makedirs(out_dir, exist_ok=True)
            out = os.path.join(out_dir, os.path.splitext(fn)[0] + ".txt")
            total += 1
            if not args.force and os.path.exists(out):
                if os.path.getmtime(out) >= os.path.getmtime(src):
                    skipped += 1
                    continue
            try:
                text = convert_json(src)
            except Exception as e:
                print(f"  [ERR] {src}: {e}")
                continue
            with open(out, "w", encoding="utf-8") as f:
                f.write(text)
            converted += 1
            src_chars += os.path.getsize(src)
            out_chars += len(text.encode("utf-8"))
            print(f"  [OK] {os.path.relpath(out, root)} ({len(text):,} 字符)")
    print(f"\n完成: 共{total} json, 转换{converted}, 跳过{skipped}")
    if src_chars:
        print(f"压缩比: {src_chars/1024:.0f}KB json → {out_chars/1024:.0f}KB txt ({out_chars/src_chars*100:.1f}%)")

if __name__ == "__main__":
    main()
