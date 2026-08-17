"""WebUI 模块单元测试 (基于真实 HTTP 服务)。"""

import json
import threading
import unittest
import urllib.error
import urllib.request

from app.webui import WebUI


class WebUITest(unittest.TestCase):
    def setUp(self):
        self.webui = WebUI(port=0)
        self.thread = threading.Thread(target=self.webui.run, daemon=True)
        self.thread.start()
        self.assertTrue(self.webui._ready.wait(5))
        self.base = f"http://127.0.0.1:{self.webui._server.server_address[1]}"

    def tearDown(self):
        self.webui.shutdown()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")

    def _post(self, path, payload=None):
        data = json.dumps(payload or {}).encode()
        req = urllib.request.Request(
            self.base + path, data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_index_serves_html(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("MiWiFi GitHub Hosts 同步", body)
        self.assertIn("/api/status", body)

    def test_status_returns_snapshot(self):
        self.webui.update(token_ok=True, expires_in_days=88, managed_entries=178)
        status, body = self._get("/api/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIsInstance(data, dict)
        self.assertTrue(data["token_ok"])
        self.assertEqual(data["expires_in_days"], 88)
        self.assertEqual(data["managed_entries"], 178)

    def test_authorize_submits_code(self):
        status, data = self._post("/api/authorize", {"url": "http://s.miwifi.com/x?code=abc123"})
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        self.assertEqual(self.webui.poll_code(), "http://s.miwifi.com/x?code=abc123")

    def test_authorize_rejects_empty(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post("/api/authorize", {"url": "  "})
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("error", json.loads(ctx.exception.read().decode("utf-8")))

    def test_sync_triggers_event(self):
        self.webui.sync_requested.clear()
        status, _ = self._post("/api/sync", {})
        self.assertEqual(status, 200)
        self.assertTrue(self.webui.sync_requested.is_set())

    def test_unknown_route_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/api/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_auth_required_flag_roundtrip(self):
        self.webui.request_authorization("https://example.com/auth")
        self.assertTrue(self.webui.snapshot()["auth_required"])
        self.webui.authorization_done()
        self.assertFalse(self.webui.snapshot()["auth_required"])


class WebUITokenAuthTest(unittest.TestCase):
    def setUp(self):
        self.webui = WebUI(port=0, token="sekrit")
        self.thread = threading.Thread(target=self.webui.run, daemon=True)
        self.thread.start()
        self.assertTrue(self.webui._ready.wait(5))
        self.base = f"http://127.0.0.1:{self.webui._server.server_address[1]}"

    def tearDown(self):
        self.webui.shutdown()

    def test_without_token_401(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.base + "/api/status", timeout=10)
        self.assertEqual(ctx.exception.code, 401)

    def test_with_query_token_ok(self):
        with urllib.request.urlopen(self.base + "/api/status?token=sekrit", timeout=10) as resp:
            self.assertEqual(resp.status, 200)

    def test_with_header_token_ok(self):
        req = urllib.request.Request(self.base + "/api/status", headers={"X-Token": "sekrit"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()
