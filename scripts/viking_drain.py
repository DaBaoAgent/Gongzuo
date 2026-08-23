# -*- coding: utf-8 -*-
"""viking 增量导出编排 v2：XHP 补漏 → BDSync 限量分批循环。
v1 教训(2026-08-23): temp 批次数是上传留存副本(只增不减), 不是待处理队列;
viking add 同步处理(~1.1s/个, 即时入库), 无需等队列, 直接用 --max-adds 控节奏。
用法: python viking_drain.py   (后台跑, 完成后 notify)
"""
import os, sys, time, subprocess, datetime

SCRIPTS = r"C:\Users\xxx13\AppData\Local\hermes\skills\media\gongzuo\scripts"
LIB = os.path.join(SCRIPTS, "lib_to_viking.py")
BATCH = 800              # BDSync 每轮上限
ROUND_SLEEP = 90         # 轮间休息秒(防瞬时过载)
MAX_ROUNDS = 12          # BDSync 最大轮数(5071/800≈7)
TOTAL_MAX = 8 * 3600     # 总时长上限 8 小时


def ts():
    return datetime.datetime.now().strftime("%m-%d %H:%M:%S")


def run_export(label, args, timeout=3600):
    print(f"[{ts()}] === {label} 开始 ===", flush=True)
    cmd = [sys.executable, LIB] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        rc = r.returncode
        out = (r.stdout or "").strip()
    except subprocess.TimeoutExpired:
        rc = "TIMEOUT"
        out = ""
    print(f"[{ts()}] === {label} 结束 rc={rc} ===", flush=True)
    for line in out.splitlines()[-6:]:
        print("  " + line, flush=True)
    return rc


XHP = ["--db", r"D:\@kaifa\gongzuo-data\xhp_kb.db",
       "--root", r"D:\自动剪辑\XHP-知识库",
       "--base", "viking://user/default/resources/xhp_knowledge",
       "--name", "XHP-知识库", "--state-name", "xhp"]
BDSYNC = ["--db", r"D:\@kaifa\gongzuo-data\file_index.db",
          "--root", r"D:\BaiduSyncdisk",
          "--base", "viking://user/default/resources/bdsync",
          "--name", "BaiduSyncdisk", "--state-name", "bdsync"]

start = time.time()
print(f"[{ts()}] 开始 (v2: 无队列等待, 直接限量循环)", flush=True)

# 1) XHP 补漏 (剩余 2 个失败/未导)
run_export("XHP-知识库 补漏", XHP)

# 2) BDSync 限量分批
for round_i in range(1, MAX_ROUNDS + 1):
    if time.time() - start > TOTAL_MAX:
        print(f"[{ts()}] !! 达总时长上限, 提前结束 (剩余留待 cron 渐进)", flush=True)
        break
    rc = run_export(f"BaiduSyncdisk 第{round_i}轮 (≤{BATCH})", BDSYNC + ["--max-adds", str(BATCH)])
    if rc == 0 and "新增/更新 0" in "":  # 无新增检测走日志行
        pass
    print(f"[{ts()}] 轮间休息 {ROUND_SLEEP}s...", flush=True)
    time.sleep(ROUND_SLEEP)

print(f"[{ts()}] 全部完成, 总耗时 {(time.time()-start)/3600:.1f}h", flush=True)
