"""Remail 接码 provider 的单元测试（全部 mock，不触网、不下真实订单）。"""

import copy
import unittest
from unittest.mock import patch

import app_config
import mail_service
from registration_flow import VerificationCodeUnavailable


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def make_clock():
    clock = {"now": 0.0}

    def now():
        return clock["now"]

    def sleep(seconds, _cancel=None):
        clock["now"] += max(float(seconds), 2.0)

    return now, sleep


class RemailPickupTests(unittest.TestCase):
    def test_official_verification_code_is_used_directly(self):
        message = {
            "id": 1,
            "sender": "noreply@x.ai",
            "recipient": "abc@outlook.com",
            "subject": "Your verification code",
            "bodyPreview": "Your code is 829-104.",
            "verificationCode": "829-104",
        }
        with patch.object(mail_service, "remail_get_messages", return_value=[message]) as get_messages:
            code = mail_service.remail_get_oai_code(
                "service-token", "abc@outlook.com", timeout=10, poll_interval=2
            )
        self.assertEqual(code, "829-104")
        get_messages.assert_called_once_with("abc@outlook.com", token="service-token")

    def test_extracts_code_from_preview_when_field_missing(self):
        message = {
            "id": 1,
            "recipient": "abc@outlook.com",
            "subject": "xAI",
            "bodyPreview": "Please enter code ABC-123 to continue.",
        }
        with patch.object(mail_service, "remail_get_messages", return_value=[message]):
            code = mail_service.remail_get_oai_code(
                "service-token", "abc@outlook.com", timeout=10, poll_interval=2
            )
        self.assertEqual(code, "ABC-123")

    def test_falls_back_to_message_detail_with_retry(self):
        message = {"id": 7, "recipient": "abc@outlook.com", "subject": "xAI", "bodyPreview": "welcome"}
        calls = {"count": 0}
        now, sleep = make_clock()

        def get_messages(address, token=None):
            return [message]

        def get_detail(message_id, address, token=None):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("temporary")
            return {"subject": "xAI", "body": "Your verification code is ABC-123 today."}

        with patch.object(mail_service, "remail_get_messages", side_effect=get_messages), \
             patch.object(mail_service, "remail_get_message_detail", side_effect=get_detail), \
             patch.object(mail_service, "sleep_with_cancel", side_effect=sleep, create=True), \
             patch.object(mail_service.time, "time", side_effect=now):
            code = mail_service.remail_get_oai_code(
                "service-token", "abc@outlook.com", timeout=10, poll_interval=2
            )
        self.assertEqual(code, "ABC-123")
        self.assertEqual(calls["count"], 2)

    def test_message_from_other_recipient_is_ignored(self):
        message = {
            "id": 1,
            "recipient": "someone@outlook.com",
            "subject": "readable code ABC-123",
            "verificationCode": "ABC-123",
        }
        now, sleep = make_clock()
        with patch.object(mail_service, "remail_get_messages", return_value=[message]), \
             patch.object(mail_service, "sleep_with_cancel", side_effect=sleep, create=True), \
             patch.object(mail_service.time, "time", side_effect=now):
            with self.assertRaises(VerificationCodeUnavailable):
                mail_service.remail_get_oai_code(
                    "service-token", "abc@outlook.com", timeout=4, poll_interval=2
                )

    def test_timeout_raises_verification_code_unavailable(self):
        now, sleep = make_clock()
        with patch.object(mail_service, "remail_get_messages", return_value=[]), \
             patch.object(mail_service, "sleep_with_cancel", side_effect=sleep, create=True), \
             patch.object(mail_service.time, "time", side_effect=now):
            with self.assertRaises(VerificationCodeUnavailable):
                mail_service.remail_get_oai_code(
                    "service-token", "abc@outlook.com", timeout=6, poll_interval=2
                )


class RemailOrderTests(unittest.TestCase):
    def test_active_order_returns_email_and_token(self):
        order = {
            "orderNo": "MT001",
            "status": "active",
            "deliveryEmail": "xyz@outlook.com",
            "serviceToken": "svc-token",
        }
        with patch.object(mail_service, "remail_create_order", return_value=order):
            address, token = mail_service.remail_get_email_and_token()
        self.assertEqual(address, "xyz@outlook.com")
        self.assertEqual(token, "svc-token")

    def test_create_order_sends_idempotency_and_mode(self):
        calls = []

        def fake_post(url, **kwargs):
            calls.append((url, kwargs))
            return FakeResponse({
                "orderNo": "MT002",
                "status": "active",
                "deliveryEmail": "zz@outlook.com",
                "serviceToken": "svc",
            })

        with patch.dict(mail_service.config, {
            "remail_api_key": "rk-test",
            "remail_project_id": "1001",
            "remail_email_suffix": "outlook.com",
        }), patch.object(mail_service, "http_post", side_effect=fake_post, create=True):
            order = mail_service.remail_create_order()
        self.assertEqual(order["orderNo"], "MT002")
        url, kwargs = calls[0]
        self.assertTrue(url.endswith("/v1/open/orders"))
        self.assertEqual(kwargs["params"], {"serviceMode": "code"})
        self.assertEqual(kwargs["json"], {"projectId": 1001, "emailSuffix": "outlook.com"})
        self.assertIn("Idempotency-Key", kwargs["headers"])
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer rk-test")

    def test_paid_order_polls_until_active(self):
        def fake_post(url, **kwargs):
            return FakeResponse({"orderNo": "MT003", "status": "paid"})

        def fake_get(url, **kwargs):
            return FakeResponse({
                "orderNo": "MT003",
                "status": "active",
                "deliveryEmail": "pd@outlook.com",
                "serviceToken": "svc",
            })

        now, sleep = make_clock()
        with patch.dict(mail_service.config, {
            "remail_api_key": "rk-test",
            "remail_project_id": "1001",
            "remail_email_suffix": "outlook.com",
        }), patch.object(mail_service, "http_post", side_effect=fake_post, create=True), \
             patch.object(mail_service, "http_get", side_effect=fake_get, create=True), \
             patch.object(mail_service, "sleep_with_cancel", side_effect=sleep, create=True), \
             patch.object(mail_service.time, "time", side_effect=now):
            address, token = mail_service.remail_get_email_and_token()
        self.assertEqual(address, "pd@outlook.com")
        self.assertEqual(token, "svc")

    def test_failed_order_raises(self):
        def fake_post(url, **kwargs):
            return FakeResponse({"orderNo": "MT004", "status": "paid"})

        def fake_get(url, **kwargs):
            return FakeResponse({
                "orderNo": "MT004",
                "status": "failed",
                "failureCode": "insufficient_inventory",
            })

        now, sleep = make_clock()
        with patch.dict(mail_service.config, {
            "remail_api_key": "rk-test",
            "remail_project_id": "1001",
            "remail_email_suffix": "outlook.com",
        }), patch.object(mail_service, "http_post", side_effect=fake_post, create=True), \
             patch.object(mail_service, "http_get", side_effect=fake_get, create=True), \
             patch.object(mail_service, "sleep_with_cancel", side_effect=sleep, create=True), \
             patch.object(mail_service.time, "time", side_effect=now):
            with self.assertRaisesRegex(Exception, "MT004"):
                mail_service.remail_get_email_and_token()

    def test_pick_project_id_uses_public_inventory(self):
        projects = [{
            "id": 1001,
            "products": [{"codeEnabled": True, "suffixes": [
                {"suffix": "outlook.com", "totalAvailable": 5, "publicAvailable": 3},
            ]}],
        }]

        class FakeResp(FakeResponse):
            def json(self):
                return {"data": {"items": projects}}

        with patch.dict(mail_service.config, {"remail_api_key": "rk-test"}), \
             patch.object(mail_service, "http_get", return_value=FakeResp(projects), create=True):
            pid = mail_service.remail_pick_project_id("outlook.com")
        self.assertEqual(pid, "1001")

    def test_pick_project_id_skips_code_disabled_products(self):
        projects = [
            {"id": 1, "products": [{"codeEnabled": False, "suffixes": [
                {"suffix": "outlook.com", "publicAvailable": 9},
            ]}]},
            {"id": 2, "products": [{"suffixes": [
                {"suffix": "outlook.com", "publicAvailable": 4},
            ]}]},
        ]

        class FakeResp(FakeResponse):
            def json(self):
                return {"data": {"items": projects}}

        with patch.dict(mail_service.config, {"remail_api_key": "rk-test"}), \
             patch.object(mail_service, "http_get", return_value=FakeResp(projects), create=True):
            pid = mail_service.remail_pick_project_id("outlook.com")
        self.assertEqual(pid, "2")


class RemailConfigTests(unittest.TestCase):
    def test_provider_in_enum_is_accepted(self):
        cfg = copy.deepcopy(app_config.DEFAULT_CONFIG)
        cfg["email_provider"] = "remail"
        cfg["remail_api_key"] = "rk-test"
        self.assertEqual(app_config.validate_run_requirements(cfg)["remail_api_key"], "rk-test")

    def test_missing_api_key_is_rejected(self):
        cfg = copy.deepcopy(app_config.DEFAULT_CONFIG)
        cfg["email_provider"] = "remail"
        cfg["remail_api_key"] = ""
        with self.assertRaisesRegex(app_config.ConfigError, "remail_api_key"):
            app_config.validate_run_requirements(cfg)

    def test_dispatch_uses_remail_provider(self):
        order = {
            "orderNo": "MT005",
            "status": "active",
            "deliveryEmail": "disp@outlook.com",
            "serviceToken": "svc",
        }
        with patch.dict(mail_service.config, {"email_provider": "remail"}), \
             patch.object(
                 mail_service, "remail_get_email_and_token",
                 return_value=(order["deliveryEmail"], order["serviceToken"]),
             ) as factory:
            address, token = mail_service.get_email_and_token()
        self.assertEqual(address, "disp@outlook.com")
        self.assertEqual(token, "svc")
        factory.assert_called_once()

    def test_dispatch_uses_remail_for_code(self):
        with patch.dict(mail_service.config, {"email_provider": "remail"}), \
             patch.object(mail_service, "remail_get_oai_code", return_value="XYZ-123") as get_code:
            code = mail_service.get_oai_code("svc", "disp@outlook.com", timeout=5)
        self.assertEqual(code, "XYZ-123")
        get_code.assert_called_once()


if __name__ == "__main__":
    unittest.main()
