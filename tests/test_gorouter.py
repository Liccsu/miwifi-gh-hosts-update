"""gorouter 客户端单元测试 (基于本地假 HTTP 服务)。"""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from app.gorouter import ApiError, GorouterClient, TokenExpiredError

TOKEN = "test-access-token"
DEVICE_ID = "test-device-id"


class FakeHandler(BaseHTTPRequestHandler):
    recorded = []

    def _record(self):
        parts = urlsplit(self.path)
        FakeHandler.recorded.append(
            {
                "method": self.command,
                "path": parts.path,
                "query": parse_qs(parts.query),
                "body": parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
                if self.command == "POST"
                else {},
            }
        )

    def _reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._record()
        self._reply(FakeHandler.responses.pop(0))

    def do_POST(self):
        self._record()
        self._reply(FakeHandler.responses.pop(0))

    def log_message(self, *args):
        pass


class GorouterClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeHandler.recorded = []
        FakeHandler.responses = []

    def _client(self):
        return GorouterClient(
            token=TOKEN,
            device_id=DEVICE_ID,
            base_url=f"http://127.0.0.1:{self.port}",
        )

    def test_get_hosts_sends_auth_params(self):
        FakeHandler.responses.append({"code": 0, "hosts": ["1.2.3.4 a.com"]})
        hosts = self._client().get_hosts()
        self.assertEqual(hosts, ["1.2.3.4 a.com"])
        req = FakeHandler.recorded[0]
        self.assertEqual(req["method"], "GET")
        self.assertEqual(req["path"], "/api-third-party/service/internal/custom_host_get")
        self.assertEqual(req["query"]["token"], [TOKEN])
        self.assertEqual(req["query"]["deviceId"], [DEVICE_ID])
        self.assertEqual(req["query"]["appId"], ["2882303761517675329"])
        self.assertEqual(req["query"]["scope"], ["1+1000+3"])

    def test_set_hosts_posts_json_entries(self):
        FakeHandler.responses.append({"code": 0, "msg": "OK"})
        self._client().set_hosts(["1.2.3.4 a.com", "5.6.7.8 b.com"])
        req = FakeHandler.recorded[0]
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["path"], "/api-third-party/service/internal/custom_host_set")
        body = req["body"]
        self.assertEqual(body["token"], [TOKEN])
        self.assertEqual(
            json.loads(body["hosts"][0]), ["1.2.3.4 a.com", "5.6.7.8 b.com"]
        )

    def test_code_3001_raises_token_expired(self):
        FakeHandler.responses.append({"code": 3001, "msg": "expired"})
        with self.assertRaises(TokenExpiredError):
            self._client().get_hosts()

    def test_nonzero_code_raises_api_error(self):
        FakeHandler.responses.append({"code": -1, "msg": "bad"})
        with self.assertRaises(ApiError):
            self._client().get_hosts()


class FakeHttpErrorHandler(FakeHandler):
    def _reply(self, payload, status=200):
        super()._reply(payload, status=401)


class Http401Test(unittest.TestCase):
    def test_http_401_raises_token_expired(self):
        FakeHandler.responses = [{"code": 0}]
        server = ThreadingHTTPServer(("127.0.0.1", 0), FakeHttpErrorHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = GorouterClient(
                token=TOKEN, device_id=DEVICE_ID, base_url=f"http://127.0.0.1:{server.server_address[1]}"
            )
            with self.assertRaises(TokenExpiredError):
                client.get_hosts()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
