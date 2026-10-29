"""邮箱池体检：列出所有邮箱并检查可用性（Outlook / iCloud 通吃）。"""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app_config
import outlook_mail
from outlook_mailbox_pool import load_outlook_mailbox_pool, parse_outlook_accounts

POOL = app_config.config.get("outlook_accounts_file", "./output/mailboxes/outlook-accounts.txt")

raw = ""
if os.path.exists(POOL):
    with open(POOL, "r", encoding="utf-8-sig") as fh:
        raw = fh.read()

accounts = parse_outlook_accounts(raw)
if not accounts:
    print(f"[!] 邮箱池里没有有效记录: {POOL}")
    print("    Outlook: powershell -ExecutionPolicy Bypass -File .\\outlook_token.ps1")
    print("    iCloud : powershell -ExecutionPolicy Bypass -File .\\icloud_pool.ps1 -Email <邮箱> -Password <app专用密码>")
    sys.exit(1)

print(f"邮箱池: {POOL}")
print(f"有效记录: {len(accounts)}\n")

for acc in accounts:
    kind = "iCloud" if outlook_mail.is_icloud_account(acc) else "Outlook"
    print(f"--- {acc.email}  [{kind}/{acc.mode}] ---")
    try:
        result = outlook_mail.probe_outlook_account(acc)
        print(f"    usable = {result.get('usable')}")
        for channel in ("imap", "graph"):
            info = result.get(channel) or {}
            if info.get("ok"):
                print(f"    {channel:6} OK  folders={info.get('folders')}")
            elif info.get("error"):
                print(f"    {channel:6} --  {info['error'][:110]}")
    except Exception as exc:
        print(f"    [!!] {type(exc).__name__}: {str(exc)[:180]}")
    print()
