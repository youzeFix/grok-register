"""批量查活逻辑单元测试（离线，不访问网络）。"""
import json
import os
import sys
import tempfile
import unittest
import sqlite3
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from account_alive import (
    check_account, classify_state, load_accounts_from_db, load_accounts_from_txt,
    run_alive_check, write_report,
)
from sso_risk import inspect_sso_account_state


def _state(code=200, url="https://grok.com/", html="", error="", transport_error=False):
    state = {
        "found": False, "bot_flag_source": None, "bot_flag_details": "", "policy": "",
        "risk": None, "event": "", "denied": False, "bot_flag_sources": [],
        "bot_flag_detail_states": [], "status_code": 0, "url": "", "error": "",
        "transport_error": False,
    }
    state.update({"status_code": code, "url": url, "error": error, "transport_error": transport_error})
    if html:
        from sso_risk import parse_grok_account_state
        state.update(parse_grok_account_state(html))
    return state


ALIVE_HTML = 'botFlagSource\\":null,botFlagDetails\\":null'
DENIED_HTML = 'botFlagSource\\":1,botFlagDetails\\":\\"flagged\\"'


class ClassifyTests(unittest.TestCase):
    def test_alive(self):
        level, _ = classify_state(_state(200, html=ALIVE_HTML))
        self.assertEqual(level, "alive")

    def test_risk_blocked(self):
        level, message = classify_state(_state(200, html=DENIED_HTML))
        self.assertEqual(level, "risk_blocked")
        self.assertEqual(message, "flagged")

    def test_sso_invalid_403(self):
        level, _ = classify_state(_state(403, error="grok.com HTTP 403"))
        self.assertEqual(level, "sso_invalid")

    def test_sso_invalid_redirect_to_signin(self):
        level, _ = classify_state(_state(200, url="https://accounts.x.ai/sign-in?redirect=grok-com"))
        self.assertEqual(level, "sso_invalid")

    def test_uncertain_5xx(self):
        level, _ = classify_state(_state(503, error="grok.com HTTP 503"))
        self.assertEqual(level, "uncertain")

    def test_network_error(self):
        level, _ = classify_state(_state(0, error="connect timeout", transport_error=True))
        self.assertEqual(level, "network_error")


class CheckAccountTests(unittest.TestCase):
    def test_check_account_maps_level(self):
        account = {"email": "a@x.com", "sso": "tok"}
        with patch("account_alive.inspect_sso_account_state", return_value=_state(200, html=ALIVE_HTML)):
            _, level, _, _ = check_account(account)
        self.assertEqual(level, "alive")


class RunAliveCheckTests(unittest.TestCase):
    def test_run_updates_db_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "test.sqlite3")
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, email TEXT, password TEXT, sso TEXT, authorization_status TEXT, updated_at TEXT)")
            conn.execute("INSERT INTO accounts (id, email, password, sso) VALUES (1, 'a@x.com', 'pw', 'tok1')")
            conn.commit()
            conn.close()
            accounts = load_accounts_from_db(db)
            self.assertEqual(len(accounts), 1)
            html_responses = {
                "tok1": _state(200, html=ALIVE_HTML),
            }
            def fake_inspect(sso, **kwargs):
                return html_responses.get(sso, _state(403))
            with patch("account_alive.inspect_sso_account_state", side_effect=fake_inspect):
                payload = run_alive_check(accounts, concurrency=1, delay_range=(0, 0), db_path=db)
            self.assertEqual(payload["summary"]["alive"], 1)
            conn = sqlite3.connect(db)
            status = conn.execute("SELECT authorization_status FROM accounts WHERE id=1").fetchone()[0]
            conn.close()
            self.assertEqual(status, "alive")
            report_path = write_report(payload, output_dir=tmp)
            self.assertTrue(os.path.exists(report_path))

    def test_txt_loader(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "accounts_test.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("a@x.com----pw----tok1\n")
                f.write("bad line\n")
            accounts = load_accounts_from_txt([path])
            self.assertEqual(len(accounts), 1)
            self.assertEqual(accounts[0]["sso"], "tok1")


if __name__ == "__main__":
    unittest.main()
