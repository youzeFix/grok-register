"""Cloud Mail 自检脚本（Cloud Mail 自建后验证 + grok-register 联调）。

用法:
    python cloudmail_check.py gen <admin_email> <admin_password>   # 生成 Public Token
    python cloudmail_check.py probe <api_base> <public_token> [domain]  # 连通性 + 收件查询
    python cloudmail_check.py mail <api_base> <public_token> <address>   # 查询某地址的邮件

说明:
    - 所有请求都通过项目自身的代理配置(走 config.json 的 proxy)，与注册流程同一出口
    - Public Token 以裸字符串放在 Authorization 头，不要加 Bearer
"""
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app_config  # noqa: E402

app_config.load_config()


def _proxy_note():
    cfg = app_config.config
    mode = cfg.get("proxy_mode", "auto")
    proxy = cfg.get("proxy", "")
    return f"proxy_mode={mode} proxy={proxy or '(直连)'}"


def _post(url, payload, headers=None, timeout=30):
    from curl_cffi import requests

    cfg = app_config.config
    proxy = ""
    try:
        from proxy_pool import current_proxy_url

        proxy = current_proxy_url() or ""
    except Exception:
        proxy = ""
    if not proxy and cfg.get("proxy_mode") == "auto":
        proxy = cfg.get("proxy", "") or ""
    proxies = {"http": proxy, "https": proxy} if proxy else None
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    kwargs = {"timeout": timeout, "headers": hdrs}
    if proxies:
        kwargs["proxies"] = proxies
    return requests.post(url, impersonate="chrome", **kwargs)


def cmd_gen(admin_email, admin_password, api_base=None):
    base = (api_base or input("api_base (如 https://mail.example.com): ")).rstrip("/")
    print(f"[*] {_proxy_note()}")
    print(f"[*] POST {base}/api/public/genToken")
    r = _post(f"{base}/api/public/genToken",
              {"email": admin_email, "password": admin_password})
    print(f"[*] HTTP {r.status_code}: {r.text[:400]}")
    try:
        data = r.json()
    except Exception:
        return 1
    token = (data.get("data") or {}).get("token")
    if token:
        print()
        print("=" * 62)
        print(f"Public Token = {token}")
        print("=" * 62)
        print("写入 config.json:")
        print(json.dumps({
            "email_provider": "cloudmail",
            "cloudmail_api_base": base,
            "cloudmail_public_token": token,
            "cloudmail_domains": input("收信域名(逗号分隔): ").strip(),
            "cloudmail_path_messages": "/api/public/emailList",
        }, ensure_ascii=False, indent=2))
        print()
        print("!! 注意: Workers KV 收敛需要约 60 秒，请等待后再测试。")
    else:
        print("[!!] 未取到 token，请检查 ADMIN 变量 / 管理员密码 / 接口地址")
        return 1
    return 0


def cmd_probe(api_base, token, domain=None):
    base = api_base.rstrip("/")
    print(f"[*] {_proxy_note()}")

    print("\n--- 1. /api/public/emailList 鉴权探测 ---")
    payload = {"toEmail": f"__probe__@{(domain or 'invalid.local')}",
               "type": 0, "isDel": 0, "timeSort": "desc", "num": 1, "size": 20}
    try:
        r = _post(f"{base}/api/public/emailList", payload,
                  headers={"Authorization": token})
        print(f"    HTTP {r.status_code}: {r.text[:300]}")
        if r.status_code == 200:
            try:
                data = r.json()
                code = data.get("code")
                print(f"    code={code} message={data.get('message')!r}")
                if code == 200:
                    print("    [OK] Public Token 有效，鉴权通过")
                else:
                    print("    [!!] 鉴权未通过（KV 可能还没收敛，等 60s 再试）")
            except Exception:
                print("    [!!] 响应非 JSON")
        elif r.status_code == 401:
            print("    [!!] 401 token验证失败 -> Public Token 不匹配或 KV 未收敛")
            print("        等 60 秒重试；若仍失败，检查 token 是否被重新生成过")
        else:
            print("    [!!] 鉴权失败")
    except Exception as exc:
        print(f"    [!!] {type(exc).__name__}: {str(exc)[:200]}")
        return 1

    print("\n--- 2. 域名可达性 ---")
    for probe in (f"{base}/", f"{base}/health"):
        try:
            from curl_cffi import requests
            r = requests.get(probe, impersonate="chrome", timeout=20)
            print(f"    GET {probe} -> HTTP {r.status_code}")
        except Exception as exc:
            print(f"    GET {probe} -> {type(exc).__name__}: {str(exc)[:80]}")
    return 0


def cmd_mail(api_base, token, address):
    base = api_base.rstrip("/")
    payload = {"toEmail": address, "type": 0, "isDel": 0,
               "timeSort": "desc", "num": 1, "size": 20}
    r = _post(f"{base}/api/public/emailList", payload,
              headers={"Authorization": token})
    print(f"HTTP {r.status_code}")
    try:
        data = r.json()
    except Exception:
        print(r.text[:500])
        return 1
    messages = data.get("data") or []
    print(f"邮件数 = {len(messages)}")
    for msg in messages[:10]:
        print(f"  - [{msg.get('emailId')}] {msg.get('sendEmail')} | {msg.get('subject')}")
        print(f"      text: {(msg.get('text') or '')[:160]}")
    if not messages:
        print("  (无邮件 —— 检查: 1) 是否开启「无人收件」 2) Email Routing catch-all 是否指向 Worker")
    return 0


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "gen":
        if len(sys.argv) < 4:
            print("用法: python cloudmail_check.py gen <admin_email> <admin_password> [api_base]")
            return 1
        return cmd_gen(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
    if cmd == "probe":
        if len(sys.argv) < 4:
            print("用法: python cloudmail_check.py probe <api_base> <public_token> [domain]")
            return 1
        return cmd_probe(sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)
    if cmd == "mail":
        if len(sys.argv) < 5:
            print("用法: python cloudmail_check.py mail <api_base> <public_token> <address>")
            return 1
        return cmd_mail(sys.argv[2], sys.argv[3], sys.argv[4])
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
