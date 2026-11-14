#!/usr/bin/env python3
"""过夜批任务看守器：断点续跑直到目标数量，超时硬停，全程带状态日志。"""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 100
DEADLINE_HOUR = int(sys.argv[2]) if len(sys.argv) > 2 else 7  # 明天 07:00 前
PER_RUN_TIMEOUT_SEC = 3600 * 3  # 单次进程最长 3h，挂死就杀
LOG = os.path.join(ROOT, "run100.log")
CONFIG = os.path.join(ROOT, "config.json")


def log(msg):
    stamp = datetime.now().strftime("%m-%d %H:%M:%S")
    line = f"[supervisor {stamp}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def sweep_orphans():
    """每轮引擎结束后清掉被杀引擎遗留的浏览器，避免下一轮双开。"""
    try:
        from browser_runtime import sweep_orphan_browsers
        orphans = sweep_orphan_browsers()
        if orphans:
            log(f"清理遗留浏览器: pid={orphans}")
    except Exception as exc:
        log(f"[!] 遗留浏览器清理异常: {exc}")


def wait_out_persistent_cooldown():
    """引擎熔断中止会写冷却文件；重启前先等完这段时间，别再空转。"""
    try:
        from registration_flow import remaining_persistent_cooldown
        remaining = remaining_persistent_cooldown()
    except Exception:
        return
    if remaining <= 0:
        return
    log(f"熔断冷却剩 {remaining}s，等待后再启动引擎…")
    deadline = time.time() + remaining
    while time.time() < deadline:
        time.sleep(min(30, max(5, deadline - time.time())))
    log("熔断冷却结束，恢复启动")


def count_success():
    total = 0
    paths = [os.path.join(ROOT, p) for p in os.listdir(ROOT)
             if p.startswith("accounts_") and p.endswith(".txt")]
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                total += sum(1 for line in f if "----" in line)
        except OSError:
            pass
    return total


def set_register_count(n):
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    if cfg.get("register_count") == n:
        return
    cfg["register_count"] = n
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


now = datetime.now()
deadline = (now + timedelta(days=1)).replace(hour=DEADLINE_HOUR, minute=0, second=0)
if (deadline - now) > timedelta(days=1):
    deadline -= timedelta(days=1)
log(f"看守启动 | 目标 {TARGET} | 硬截止 {deadline:%m-%d %H:%M} | 已有 {count_success()} 条")

while time.time() < deadline.timestamp():
    success = count_success()
    if success >= TARGET:
        log(f"已达目标 {TARGET}，看守退出")
        break
    need = TARGET - success
    set_register_count(need)
    log(f"启动注册: 需 {need} 条 | 已 {success}")
    wait_out_persistent_cooldown()
    started = time.time()
    try:
        proc = subprocess.Popen(
            [os.path.join(ROOT, ".venv/bin/python"), os.path.join(ROOT, "grok_register_ttk.py"), "cli"],
            cwd=ROOT, stdin=subprocess.PIPE,
            stdout=open(LOG, "ab"), stderr=subprocess.STDOUT,
        )
        proc.stdin.write(b"start\n")
        proc.stdin.flush()
        proc.stdin.close()
    except Exception as exc:
        log(f"[!] 启动异常: {exc}")
        time.sleep(30)
        continue
    while proc.poll() is None and time.time() < deadline.timestamp() and time.time() - started < PER_RUN_TIMEOUT_SEC:
        time.sleep(20)
    if proc.poll() is None:
        log("[!] 进程超时（挂死），先优雅停止（SIGINT 会触发浏览器清理），5s 后再升级强杀")
        try:
            proc.send_signal(signal.SIGINT)
        except Exception:
            pass
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
    else:
        log(f"进程退出码 {proc.returncode}")
    sweep_orphans()
    success = count_success()
    log(f"本轮结束 | 当前成功 {success}/{TARGET} | 休眠 15s")
    time.sleep(15)
else:
    log("到达硬截止时间，停止。")

log(f"看守结束 | 最终成功 {count_success()}")
