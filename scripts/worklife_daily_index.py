# -*- coding: utf-8 -*-
"""工作生活文件夹 每日增量抓取+导出Viking 合并 watchdog（cron 用）

每天 01:00 由 Hermes cron 调用，处理两个工作生活文件夹：
A. D:\\BaiduSyncdisk      → file_index.db  (gongzuo 主库)
B. D:\\自动剪辑\\XHP-知识库 → xhp_kb.db

每库流程：
1. doc_indexer.py 增量扫描 + 图片 OCR（--ocr-media，断点续传）
2. 智谱图片场景描述（--vision-desc-zhipu，缺口模式）
3. 智谱视频场景描述（--vision-video-zhipu，头尾各1帧）
4. 视频 OCR（--ocr-video，头尾各1帧）
5. lib_to_viking.py 导出到 OpenViking（参数化通用版，自身增量）

输出规则（no_agent watchdog 模式）：
- 有新增/变更 → 输出摘要（投递通知）
- 有失败 → 输出警报（exit 1）
- 无变化 → 静默（stdout 为空，不打扰）
"""
import os
import re
import subprocess
import sys
import json
import time

HERE = r"C:\Users\xxx13\AppData\Local\hermes\skills\media\gongzuo\scripts"  # 技能 scripts 绝对路径
INDEXER = os.path.join(HERE, "doc_indexer.py")
EXPORTER = os.path.join(HERE, "lib_to_viking.py")
HERMES_EXPORT = r"C:\Users\xxx13\AppData\Local\hermes\scripts\hermes_to_viking.py"  # 会话→Viking
ASSEMBLER = r"C:\Users\xxx13\AppData\Local\hermes\scripts\gongzuo_assembly.py"  # 数据汇总→7份汇编md
SKILL_LIST = r"C:\Users\xxx13\AppData\Local\hermes\scripts\gen_skill_list.py"  # 06-后装技能清单
ENV_FILE = r"C:\Users\xxx13\AppData\Local\hermes\.env"
LOCK_FILE = r"D:\@kaifa\gongzuo-data\worklife_daily.lock"  # 与 04:00 gongzuo 任务的并发互斥锁

# 库定义: (显示名, root, db, viking base uri, state_name[对齐旧state])
LIBRARIES = [
    ("BaiduSyncdisk", r"D:\BaiduSyncdisk",
     r"D:\@kaifa\gongzuo-data\file_index.db",
     "viking://user/default/resources/bdsync", "bdsync"),
    ("XHP知识库", r"D:\自动剪辑\XHP-知识库",
     r"D:\@kaifa\gongzuo-data\xhp_kb.db",
     "viking://user/default/resources/xhp_knowledge", "xhp"),
]
TIMEOUT_INDEX = 5400   # 索引+OCR 90 分钟
TIMEOUT_EXPORT = 21600  # 导出 6 小时（首次全量 9696 文件需 ~2 小时提交）


def load_env():
    """加载 .env 到 os.environ（cron 环境可能没设 ZHIPU_API_KEY）"""
    try:
        with open(ENV_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass


def run_indexer(root, db, args, label):
    """跑 doc_indexer，返回 (stdout+stderr, returncode)"""
    try:
        r = subprocess.run(
            [sys.executable, "-u", INDEXER, "--root", root, "--db", db] + args,
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            timeout=TIMEOUT_INDEX,
        )
        return ((r.stdout or "") + (r.stderr or "")), r.returncode
    except subprocess.TimeoutExpired:
        return f"{label} 超时（>{TIMEOUT_INDEX}s）", -1


def migrate_state(state_name, db):
    """state 文件迁移到 db 同目录（2026-08-23 优化：state 不再放技能目录）。

    旧位置依次检查：①技能 scripts（刚改版前）②hermes/scripts（旧全量导出脚本时代）。
    若 db 同目录无 state 而旧位置有，先复制，避免重跑全量。
    """
    state_dir = os.path.dirname(os.path.abspath(db))
    here = os.path.dirname(os.path.abspath(__file__))  # 技能 scripts
    old_dirs = [here, r"C:\Users\xxx13\AppData\Local\hermes\scripts"]
    for loc in ("state.json", "lock"):
        new_f = os.path.join(state_dir, f"{state_name}_to_viking.{loc}")
        if os.path.exists(new_f):
            continue
        for old_dir in old_dirs:
            old_f = os.path.join(old_dir, f"{state_name}_to_viking.{loc}")
            if os.path.exists(old_f):
                try:
                    import shutil
                    shutil.copy2(old_f, new_f)
                    print(f"  [state迁移] {old_f} → {new_f}", flush=True)
                except Exception as e:
                    print(f"  [state迁移失败] {e}", flush=True)
                break


def run_export(exporter_args, label):
    """跑 lib_to_viking 导出，返回 (输出, 有无新增, returncode)"""
    try:
        r = subprocess.run(
            [sys.executable, "-u", EXPORTER] + exporter_args,
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            timeout=TIMEOUT_EXPORT,
        )
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return out, ("增量导出完成" in out), r.returncode
    except subprocess.TimeoutExpired:
        return f"{label} 导出超时", False, -1


def process_library(name, root, db, base_uri, state_name):
    """处理一个库：索引+OCR+描述+导出，返回 (msgs, alerts)"""
    msgs, alerts = [], []

    # 1. 增量扫描 + 图片 OCR
    out, rc = run_indexer(root, db, ["--workers", "8", "--ocr-media"], name + " 索引+OCR")
    m = re.search(
        r"完成: 共\d+ 正文索引=(\d+) 媒体元数据=(\d+) 其余元数据=(\d+) 跳过=(\d+) 失败=(\d+)",
        out,
    )
    new_docs = new_media = meta = skipped = failed = 0
    if m:
        new_docs, new_media, meta, skipped, failed = map(int, m.groups())
    if rc != 0 or failed > 0 or not m:
        alerts.append(f"{name} 索引异常(failed={failed} rc={rc})")
    ocr_m = re.search(r"OCR pass 完成: 识别=(\d+) 无文字=(\d+) 失败=(\d+) 跳过=(\d+)", out)
    if ocr_m:
        ocr_ok, ocr_no_text, ocr_failed, ocr_skip = map(int, ocr_m.groups())
        if ocr_failed > 0:
            alerts.append(f"{name} 图片OCR失败 {ocr_failed}")
        if ocr_ok + ocr_no_text > 0:
            msgs.append(f"{name} 图片OCR {ocr_ok}张+{ocr_no_text}无文字")

    # 2. 智谱图片场景描述（缺口模式，断点续传）
    out, rc = run_indexer(root, db, ["--vision-desc-zhipu"], name + " 图片描述")
    v_m = re.search(r"VISION-ZHIPU 耗时 .*-> \{'ok': (\d+), 'failed': (\d+)", out)
    if v_m:
        v_ok, v_failed = map(int, v_m.groups())
        if v_failed > 0:
            alerts.append(f"{name} 图片描述失败 {v_failed}")
        if v_ok > 0:
            msgs.append(f"{name} 图片描述 {v_ok}张")

    # 3. 智谱视频场景描述（头尾各1帧）
    out, rc = run_indexer(root, db, ["--vision-video-zhipu"], name + " 视频描述")
    vv_m = re.search(r"视频VISION-ZHIPU 耗时 .*-> \{'ok': (\d+), 'failed': (\d+), 'frames': (\d+)", out)
    if vv_m:
        vv_ok, vv_failed, vv_frames = map(int, vv_m.groups())
        if vv_failed > 0:
            alerts.append(f"{name} 视频描述失败 {vv_failed}")
        if vv_ok > 0:
            msgs.append(f"{name} 视频描述 {vv_ok}个({vv_frames}帧)")

    # 4. 视频 OCR（头尾各1帧）
    out, rc = run_indexer(root, db, ["--ocr-video"], name + " 视频OCR")
    vo_m = re.search(r"视频OCR 耗时 .*-> \{'ok': (\d+), 'no_text': (\d+), 'failed': (\d+)", out)
    if vo_m:
        vo_ok, vo_no_text, vo_failed = map(int, vo_m.groups())
        if vo_failed > 0:
            alerts.append(f"{name} 视频OCR失败 {vo_failed}")
        if vo_ok + vo_no_text > 0:
            msgs.append(f"{name} 视频OCR {vo_ok}个+{vo_no_text}无文字")

    # 5. 导出到 Viking（自身增量）
    if not alerts:  # 索引正常才导出
        migrate_state(state_name, db)  # 旧 state 迁移（技能目录/hermes/scripts → db同目录），避免重跑全量
        eout, has_new, erc = run_export(
            ["--db", db, "--root", root, "--base", base_uri,
             "--name", name, "--state-name", state_name],
            name + " 导出")
        if erc != 0:
            alerts.append(f"{name} Viking导出异常(rc={erc}): {eout[-200:]}")
        elif has_new:
            msgs.append(f"{name} 导出Viking: " + eout.split("增量导出完成: ")[-1])

    # 新增汇总
    new_total = new_docs + new_media + meta
    if new_total > 0:
        msgs.insert(0, f"{name} 新增 {new_docs}文档+{new_media}媒体+{meta}其他, 跳过{skipped}")
    return msgs, alerts


def _pid_alive(pid: int) -> bool:
    """Windows 下检测 PID 是否存活"""
    try:
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:
        return True  # 无法判断时保守认为存活


def acquire_lock() -> bool:
    """写互斥锁。若已有锁且 PID 存活 → 返回 False（另一实例在跑，退出）。
    锁文件为 JSON: {"pid": N, "started": "..."}。"""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            pid = data.get("pid")
            if pid and _pid_alive(pid):
                print(f"⚠ 已有 worklife 实例在运行 (pid={pid})，本次跳过", flush=True)
                return False
        except Exception:
            pass
        # 死锁残留（PID 不存在或损坏）→ 删除后重写
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass
    with open(LOCK_FILE, "w", encoding="utf-8") as f:
        json.dump({"pid": os.getpid(), "started": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
    return True


def release_lock():
    try:
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)
    except OSError:
        pass


def main():
    if not acquire_lock():
        return 1
    try:
        return _main()
    finally:
        release_lock()


def _main():
    load_env()
    all_msgs, all_alerts = [], []
    for name, root, db, base_uri, state_name in LIBRARIES:
        msgs, alerts = process_library(name, root, db, base_uri, state_name)
        all_msgs.extend(msgs)
        all_alerts.extend(alerts)

    # Hermes 会话 → Viking（自身增量：有新消息打印，无则静默）
    try:
        rh = subprocess.run(
            [sys.executable, "-u", HERMES_EXPORT],
            capture_output=True, text=True, encoding="utf-8", errors="ignore",
            timeout=1800,
        )
        hout = ((rh.stdout or "") + (rh.stderr or "")).strip()
        if "增量导出完成" in hout:
            all_msgs.append("会话导出Viking: " + hout.split("增量导出完成: ")[-1])
        elif "OpenViking 不可达" in hout:
            all_alerts.append("会话导出Viking: OpenViking 不可达")
    except Exception as e:
        all_alerts.append(f"会话导出Viking异常 {e}")

    # 数据汇总（原 04:00 gongzuo_daily_index 职责并入）：汇编 7 份 md + 06-后装技能清单
    # 仅当索引链路无异常时执行（汇编依赖库数据；有 alert 说明库可能不完整）
    if not all_alerts:
        # 汇编：收集新增数据整合进 @数据汇总 7 份 md
        try:
            ra = subprocess.run(
                [sys.executable, "-u", ASSEMBLER, "--db", LIBRARIES[0][2]],
                capture_output=True, text=True, encoding="utf-8", errors="ignore",
                timeout=300,
            )
            aout = ((ra.stdout or "") + (ra.stderr or "")).strip()
            if aout and "无新增" not in aout:
                all_msgs.append("数据汇总: " + aout)
        except Exception as e:
            all_alerts.append(f"数据汇总异常 {e}")
        # 06-后装技能清单：每日全量重生成（技能增删/使用次数变化都能反映）
        try:
            rs = subprocess.run(
                [sys.executable, "-u", SKILL_LIST],
                capture_output=True, text=True, encoding="utf-8", errors="ignore",
                timeout=300,
            )
            sout = ((rs.stdout or "") + (rs.stderr or "")).strip()
            if sout:
                all_msgs.append("06-技能清单: " + sout)
        except Exception as e:
            all_alerts.append(f"06-技能清单异常 {e}")

    if all_alerts:
        print("⚠ 工作生活文件夹增量：" + "；".join(all_alerts))
        return 1
    if all_msgs:
        print("📂 工作生活文件夹增量：" + "；".join(all_msgs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
