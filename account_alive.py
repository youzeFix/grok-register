"""存量账号批量查活：遍历账号库，用 sso cookie 访问 grok.com 判定存活状态。

状态分级:
  alive          — HTTP 200 且未命中风控（botFlag/policy=deny）
  risk_blocked   — HTTP 200 但命中风控（botFlagSource∈{1,2} 或 policy=deny）
  sso_invalid    — HTTP 401/403 或被跳转到登录/注册页（sso 失效）
  uncertain      — 其它 HTTP 状态（429/5xx 等，不判死）
  network_error  — 传输失败（超时/连接错误，不判死，可重查）
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from sso_risk import inspect_sso_account_state, registration_risk_should_block

DEFAULT_CONCURRENCY = 3
DB_PATH = os.path.join("data", "grok-register.sqlite3")


def load_accounts_from_db(db_path=DB_PATH):
    """从 SQLite 读取账号 (id, email, password, sso, authorization_status)。"""
    if not os.path.exists(db_path):
        return []
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, email, password, sso, COALESCE(authorization_status, '') FROM accounts ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": r[0], "email": r[1], "password": r[2], "sso": r[3], "authorization_status": r[4]}
        for r in rows
    ]


def load_accounts_from_txt(paths):
    """从 accounts_*.txt（email----password----sso）读取账号。"""
    accounts = []
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if "----" not in line:
                        continue
                    parts = line.split("----")
                    if len(parts) < 3:
                        continue
                    accounts.append({
                        "id": None,
                        "email": parts[0].strip(),
                        "password": parts[1].strip(),
                        "sso": parts[2].strip(),
                        "authorization_status": "",
                        "source_file": path,
                    })
        except OSError:
            continue
    return accounts


def classify_state(state):
    """把 inspect_sso_account_state 的结果映射为查活分级。"""
    code = int(state.get("status_code") or 0)
    error = str(state.get("error") or "")
    if state.get("transport_error") or (code == 0 and error and "HTTP" not in error):
        return "network_error", error
    if code in (401, 403):
        return "sso_invalid", error or ("grok.com HTTP %s" % code)
    url = str(state.get("url") or "").lower()
    if code == 200 and ("sign-in" in url or "/sign-up" in url or "auth.x.ai" in url):
        return "sso_invalid", "被跳转到登录页（sso 已失效）"
    if code != 200:
        return "uncertain", error or ("grok.com HTTP %s" % code)
    if not state.get("found"):
        return "uncertain", "页面未发现账号状态字段（可能改版）"
    blocked, details = registration_risk_should_block(state)
    if blocked:
        return "risk_blocked", details
    return "alive", "正常"


def check_account_via_browser(page, sso, timeout=40, challenge_helper=None, log_callback=None, node_switch_helper=None):
    """用真实 Chromium 检查账号状态（curl_cffi 指纹已被 grok.com TLS 拒绝，改走浏览器）。

    challenge_helper: Cloudflare 质询出现时的自动点击回调（如注册浏览器的
    _attempt_turnstile_click），每轮质询等待期间会周期性调用。
    node_switch_helper: 导航遇 ERR 页/空白页时的换一级节点回调（Clash 紧急切换）。
    返回与 check_account 兼容的 (level, message, state)。
    """
    token = str(sso or "").strip()
    if not token:
        return "sso_invalid", "sso 为空", {}
    tab = None
    try:
        browser = page.browser
        # MixTab 的 page.html/run_js 存在 Session/Web 两半不同步的坑，
        # 这里为每次检查开一个纯净 WebTab，用完即关
        tab = browser.new_tab()
    except Exception as exc:
        return "network_error", "无法创建检查用标签页: %s" % safe_error(exc), {}

    def _check_with_tab(tab_page):
        for stale_name in ("x-userid",):
            try:
                tab_page.run_cdp("Network.deleteCookies", name=stale_name, domain=".grok.com")
            except Exception:
                pass
        try:
            tab_page.set.cookies([{"name": "sso", "value": token, "domain": ".grok.com", "path": "/"}])
        except Exception as exc:
            return "network_error", "cookie 写入失败: %s" % safe_error(exc), {}
        # 打开 grok.com；遇到 ERR 页（一级出口波动）/空白页时自动换一级节点重试
        nav_ok = False
        for nav_attempt in range(4):
            try:
                tab_page.get("https://grok.com/", retry=1, interval=1, timeout=timeout)
                tab_page.wait.doc_loaded(timeout=timeout)
            except Exception as exc:
                if log_callback:
                    log_callback("[Debug] 查活导航异常: %s" % safe_error(exc))
            try:
                probe_inner = str(tab_page.run_js("return document.body ? document.body.innerText : '';") or "").lower()
                probe_url = str(tab_page.url or "").lower()
            except Exception:
                probe_inner, probe_url = "", ""
            bad = (
                not probe_inner.strip()
                or "this site can't be reached" in probe_inner
                or "this site can\u2019t be reached" in probe_inner
                or "err_" in probe_inner
                or "about:blank" in probe_url
            )
            if not bad:
                nav_ok = True
                break
            if log_callback:
                log_callback("[查活] 导航失败（一级出口波动），尝试切换节点重试 %d/3" % (nav_attempt + 1))
            if node_switch_helper:
                try:
                    node_switch_helper()
                except Exception:
                    pass
            time.sleep(2)
        if not nav_ok:
            return "network_error", "grok.com 打开失败（一级出口连续不可达，已尝试换节点）", {}
        if os.environ.get("CHECK_ALIVE_DEBUG"):
            try:
                _dbg_url = str(tab_page.url)
                _dbg_html = str(tab_page.run_js("return document.documentElement.outerHTML") or "")[:120].replace("\n", " ")
                if log_callback:
                    log_callback("[Debug] 查活诊断: tab.url=%s | html=%s" % (_dbg_url[:60], _dbg_html))
            except Exception as _dbg_exc:
                if log_callback:
                    log_callback("[Debug] 查活诊断失败: %s" % safe_error(_dbg_exc))

        # Cloudflare 质询处理：等待自动通过（turnstile 自动点击 + clearance 写入）
        challenge_deadline = time.time() + 50
        challenged = False
        while time.time() < challenge_deadline:
            try:
                inner_probe = str(tab_page.run_js("return document.body ? document.body.innerText : '';") or "").lower()
            except Exception:
                inner_probe = ""
            if not any(m in inner_probe for m in (
                "just a moment", "verify you are human", "checking your browser",
                "attention required", "请稍候", "确认您是真人",
            )):
                break
            challenged = True
            if challenge_helper:
                try:
                    challenge_helper()
                except Exception:
                    pass
            cleared = False
            try:
                cookies = tab_page.cookies(all_domains=True) if hasattr(tab_page, "cookies") else []
                cleared = any(c.get("name") == "cf_clearance" for c in cookies or [])
            except Exception:
                cleared = False
            if cleared:
                try:
                    tab_page.get("https://grok.com/", retry=1, interval=1, timeout=timeout)
                    tab_page.wait.doc_loaded(timeout=timeout)
                except Exception:
                    pass
                break
            time.sleep(3)
        if challenged and log_callback:
            log_callback("[查活] Cloudflare 质询已处理，重新判定")

        url = ""
        html = ""
        try:
            url = str(tab_page.url or "")
            html = str(tab_page.run_js("return document.documentElement.outerHTML") or "")
        except Exception as exc:
            return "network_error", "页面读取失败: %s" % safe_error(exc), {}
        lowered = url.lower()
        if "sign-in" in lowered or "/sign-up" in lowered or "auth.x.ai" in lowered:
            return "sso_invalid", "被跳转到登录页（sso 已失效）", {"url": url}
        state = dict(_EMPTY_STATE)
        state["url"] = url
        if not html:
            return "uncertain", "页面内容为空", state
        from sso_risk import parse_grok_account_state
        parsed = parse_grok_account_state(html)
        state.update(parsed)
        if parsed.get("found"):
            blocked, details = registration_risk_should_block(state)
            if blocked:
                return "risk_blocked", details, state
            return "alive", "正常", state
        # grok.com 新版前端已不再嵌入 botFlag 字段——用登录态特征判定存活
        inner = ""
        has_userid = False
        for _ in range(10):
            try:
                inner = str(tab_page.run_js("return document.body ? document.body.innerText : '';") or "").lower()
            except Exception:
                inner = ""
            if len(inner) > 120 or "sign in" in inner or "log into" in inner or "new chat" in inner:
                break
            try:
                cookies = tab_page.cookies(all_domains=True) if hasattr(tab_page, "cookies") else []
                has_userid = any(c.get("name") == "x-userid" for c in cookies or [])
                if has_userid:
                    break
            except Exception:
                pass
            time.sleep(1)
        try:
            cookies = tab_page.cookies(all_domains=True) if hasattr(tab_page, "cookies") else []
            has_userid = has_userid or any(c.get("name") == "x-userid" for c in cookies or [])
        except Exception:
            pass
        if has_userid or ("new chat" in inner or "getting started with grok" in inner):
            return "alive", "正常（登录态确认；新版页面无风控字段）", state
        if inner.strip() and ("sign in" in inner or "log into" in inner or "create an account" in inner):
            return "sso_invalid", "页面显示登录入口（sso 已失效）", state
        cookie_names = []
        try:
            cookies = tab_page.cookies(all_domains=True) if hasattr(tab_page, "cookies") else []
            cookie_names = [c.get("name") for c in (cookies or [])][:12]
        except Exception:
            pass
        return ("uncertain",
                "页面未发现账号状态字段（可能改版）| url=%s | inner_len=%d | inner=%s | cookies=%s | html_head=%s"
                % (str(url)[:60], len(inner), inner[:130].replace("\n", " "), ",".join(cookie_names), html[:120].replace("\n", " ")),
                state)

    try:
        return _check_with_tab(tab)
    finally:
        try:
            tab.close()
        except Exception:
            pass


def safe_error(exc):
    return str(exc)[:120].replace("\n", " ")


_EMPTY_STATE = {
    "found": False, "bot_flag_source": None, "bot_flag_details": "", "policy": "",
    "risk": None, "event": "", "denied": False, "bot_flag_sources": [],
    "bot_flag_detail_states": [], "status_code": 0, "url": "", "error": "",
    "transport_error": False,
}


def check_account(account, proxy="", user_agent="", timeout=25, http_get=None):
    """检查单个账号，返回 (account, level, message, state)。"""
    state = inspect_sso_account_state(
        account.get("sso") or "",
        proxy=proxy,
        user_agent=user_agent,
        timeout=timeout,
        http_get=http_get,
    )
    level, message = classify_state(state)
    return account, level, message, state


def _update_db_status(db_path, account_id, level, checked_at):
    if account_id is None:
        return
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "UPDATE accounts SET authorization_status = ?, updated_at = ? WHERE id = ?",
                (level, checked_at, account_id),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def run_alive_check(
    accounts,
    proxy="",
    user_agent="",
    concurrency=DEFAULT_CONCURRENCY,
    delay_range=(2.0, 5.0),
    db_path=DB_PATH,
    log_callback=None,
    cancel_callback=None,
    http_get=None,
    browser_page=None,
    challenge_helper=None,
    node_switch_helper=None,
):
    """并发查活，返回汇总 dict（不删除任何账号，只更新 SQLite 状态与报告）。

    browser_page 传入时走真实浏览器通道（curl_cffi 指纹被 grok.com 拒绝的场景），
    此时并发固定为 1（同一浏览器实例顺序检查）。
    """
    if browser_page is not None:
        concurrency = 1
    summary = {"total": 0, "alive": 0, "risk_blocked": 0, "sso_invalid": 0, "uncertain": 0, "network_error": 0}
    results = []
    lock = threading.Lock()

    def _log(message):
        if log_callback:
            log_callback(message)

    def _worker(item):
        index, account = item
        if cancel_callback and cancel_callback():
            return None
        # 抖动间隔，避免固定节拍的连击
        if index > 0:
            delay = random.uniform(*delay_range)
            for _ in range(int(delay)):
                if cancel_callback and cancel_callback():
                    return None
                time.sleep(1)
            time.sleep(delay - int(delay))
        checked_at = datetime.now().isoformat(timespec="seconds")
        try:
            if browser_page is not None:
                level, message, state = check_account_via_browser(
                    browser_page,
                    account.get("sso"),
                    challenge_helper=challenge_helper,
                    node_switch_helper=node_switch_helper,
                    log_callback=log_callback,
                )
            else:
                account, level, message, state = check_account(
                    account, proxy=proxy, user_agent=user_agent, http_get=http_get,
                )
        except Exception as exc:
            account = dict(account)
            level, message, state = "network_error", str(exc), {}
        record = {
            "email": account.get("email"),
            "account_id": account.get("id"),
            "level": level,
            "message": message,
            "checked_at": checked_at,
            "status_code": int(state.get("status_code") or 0) if state else 0,
            "bot_flag_source": state.get("bot_flag_source") if state else None,
            "policy": state.get("policy") if state else "",
            "bot_flag_details": state.get("bot_flag_details") if state else "",
        }
        with lock:
            summary["total"] += 1
            summary[level] = summary.get(level, 0) + 1
            results.append(record)
            _update_db_status(db_path, account.get("id"), level, checked_at)
            _log(
                "[查活] %s -> %s%s" % (
                    account.get("email"),
                    level,
                    "" if level == "alive" else " | %s" % message[:80],
                )
            )
        return record

    with ThreadPoolExecutor(max_workers=max(1, int(concurrency))) as pool:
        futures = [pool.submit(_worker, item) for item in enumerate(accounts)]
        for future in as_completed(futures):
            future.result()

    return {"summary": summary, "results": sorted(results, key=lambda r: r["email"] or "")}


def write_report(payload, output_dir="."):
    os.makedirs(output_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(output_dir, "account_alive_report_%s.json" % stamp)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path
