"""
Clash 节点连通探测/紧急切换的单元测试（全离线，monkeypatch _request）。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import clash_rotate

SETTINGS = {
    "clash_api_url": "http://127.0.0.1:9097",
    "clash_api_secret": "secret",
    "clash_proxy_group": "宝妈云",
    "clash_rotate_nodes": "",
}

GROUP_NAME = "宝妈云"


def _group_detail(now):
    return {"proxies": {GROUP_NAME: {"type": "Selector", "all": ["A", "B"], "now": now}}}


def _setup(monkey, nodes, delays):
    """nodes/ delays: 节点 -> delay（-1 表示抛异常，0 视为不通）。"""
    group_names = {GROUP_NAME, "GLOBAL"}
    proxies = {
        GROUP_NAME: {"type": "Selector", "all": list(nodes), "now": nodes[0] if nodes else ""},
        "GLOBAL": {"type": "Selector", "all": [GROUP_NAME], "now": GROUP_NAME},
    }
    for node in nodes:
        proxies[node] = {"type": "Hysteria2", "all": []}

    def fake_request(url, secret, method="GET", payload=None, timeout=6):
        import urllib.parse
        if url.endswith("/proxies"):
            return {"proxies": proxies}
        if "/delay?" in url:
            node = url.split("/proxies/")[1].split("/delay?")[0]
            name = urllib.parse.unquote(node)
            if name not in nodes:
                raise OSError("no such proxy")
            delay = delays[name]
            if delay < 0:
                raise OSError("probe transport dead")
            return {"delay": delay}
        if GROUP_NAME in urllib.parse.unquote(url):
            return proxies[GROUP_NAME]
        if method == "PUT" and GROUP_NAME in url:
            new = (payload or {}).get("name")
            if new:
                proxies[GROUP_NAME]["now"] = new
            return {}
        raise AssertionError("unexpected call: " + url)

    monkey.side_effect = fake_request


class ProbeRotationTests(unittest.TestCase):
    def setUp(self):
        clash_rotate._node_blacklist.clear()
        with clash_rotate._probe_cache_lock:
            clash_rotate._probe_cache.clear()
        with clash_rotate._lock:
            clash_rotate._last_rotate["all_dead"] = False

    def test_probe_alive_uses_delay_api(self):
        import json as _json
        from unittest.mock import MagicMock
        fake = MagicMock()
        _setup(fake, ["A", "B"], {"A": 321, "B": 0})
        with patch.object(clash_rotate, "_request", fake):
            self.assertTrue(clash_rotate.probe_node(SETTINGS, "A"))
            self.assertFalse(clash_rotate.probe_node(SETTINGS, "B"))
            # 探测 URL 指向注册站
            called_delay = [call for call in fake.call_args_list if "/delay?" in (call[0][0] if call[0] else "")]
            self.assertTrue(called_delay and "accounts.x.ai" in called_delay[0][0][0])

    def test_rotate_next_skips_dead_nodes(self):
        fake = _prep = None
        from unittest.mock import MagicMock
        fake = MagicMock()
        # A 探测不通, B 连通 → 应切到 B 而不是 A
        _setup(fake, ["A", "B"], {"A": 0, "B": 250})
        fake.return_value = {}
        with patch.object(clash_rotate, "_request", fake):
            prev, node = clash_rotate.rotate_next(SETTINGS)
            self.assertEqual(node, "B")
            self.assertFalse(clash_rotate.was_all_dead())

    def test_rotate_next_all_dead_sets_flag(self):
        from unittest.mock import MagicMock
        fake = MagicMock()
        _setup(fake, ["A", "B"], {"A": 0, "B": -1})
        with patch.object(clash_rotate, "_request", fake):
            prev, node = clash_rotate.rotate_next(SETTINGS)
            self.assertIsNone(node)
            self.assertTrue(clash_rotate.was_all_dead())

    def test_emergency_switch_blacklists_dead_and_switches(self):
        from unittest.mock import MagicMock
        fake = MagicMock()
        _setup(fake, ["A", "B"], {"A": 0, "B": 150})
        with patch.object(clash_rotate, "_request", fake):
            status, node = clash_rotate.emergency_switch(SETTINGS, dead_node="A")
            self.assertEqual(status, "switched")
            self.assertEqual(node, "B")
            self.assertIn("A", clash_rotate._node_blacklist)
            # 已切到探测连通的 B（proxies[GROUP_NAME]["now"] 由桩维护）
            self.assertEqual(fake.call_count > 0, True)

    def test_emergency_switch_cross_group_fallback(self):
        from unittest.mock import MagicMock, call
        fake = MagicMock()
        # 组内只有 A（死），但 discover_all_nodes 能扫到 B（活的，其它订阅合并场景）
        _setup(fake, ["A"], {"A": 0})
        original = clash_rotate.discover_all_nodes

        def fake_discover(settings):
            return ["B"]

        def fake_probe(settings, node, log_callback=None):
            return node == "B"

        with patch.object(clash_rotate, "_request", fake):
            with patch.object(clash_rotate, "discover_all_nodes", fake_discover):
                with patch.object(clash_rotate, "probe_node", fake_probe):
                    status, node = clash_rotate.emergency_switch(SETTINGS, dead_node="A")
        self.assertEqual((status, node), ("switched", "B"))

    def test_emergency_switch_all_dead(self):
        fake = None
        from unittest.mock import MagicMock
        fake = MagicMock()
        _setup(fake, ["A", "B"], {"A": 0, "B": 0})
        with patch.object(clash_rotate, "_request", fake):
            status, node = clash_rotate.emergency_switch(SETTINGS, dead_node="A")
            self.assertEqual((status, node), ("all_dead", None))

    def test_blacklisted_node_is_skipped_by_emergency(self):
        from unittest.mock import MagicMock
        fake = MagicMock()
        _setup(fake, ["A", "B"], {"A": 0, "B": 0})
        clash_rotate._node_blacklist["A"] = 9e12  # 永久拉黑 → 不应被探测复活
        with patch.object(clash_rotate, "_request", fake):
            status, node = clash_rotate.emergency_switch(SETTINGS)
        self.assertEqual((status, node), ("all_dead", None))


class PersistentCooldownTests(unittest.TestCase):
    def setUp(self):
        rf = clash_rotate  # 仅为模块加载后延迟导入 registration_flow 的占位
        import registration_flow as flow
        self.flow = flow
        flow.clear_persistent_cooldown()

    def tearDown(self):
        self.flow.clear_persistent_cooldown()

    def test_note_and_remaining(self):
        self.assertEqual(self.flow.remaining_persistent_cooldown(), 0)
        self.flow.note_persistent_cooldown(600, "unit-test")
        remaining = self.flow.remaining_persistent_cooldown()
        self.assertTrue(599 <= remaining <= 600)
        self.assertEqual(self.flow.remaining_persistent_cooldown(), remaining)

    def test_clear_resets(self):
        self.flow.note_persistent_cooldown(600)
        self.flow.clear_persistent_cooldown()
        self.assertEqual(self.flow.remaining_persistent_cooldown(), 0)


if __name__ == "__main__":
    unittest.main()
