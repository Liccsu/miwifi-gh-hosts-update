"""main 模块单元测试: token 缓存、失效自动刷新与授权链接模式。"""

import json
import os
import tempfile
import unittest
from unittest import mock

from app import main
from app.gorouter import TokenExpiredError
from app.hostsfile import parse_hosts, total_length
from app.main import TokenStore, parse_authorize_input, run_sync_with_refresh, sync_once


class TokenStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "token.json")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)

    def test_roundtrip(self):
        store = TokenStore(self.path)
        store.save("tok-abc", "1+1000+3")
        data = store.load()
        self.assertEqual(data["token"], "tok-abc")
        self.assertEqual(data["scope"], "1+1000+3")
        self.assertIn("issued_at", data)

    def test_missing_file_returns_none(self):
        self.assertIsNone(TokenStore(self.path).load())

    def test_corrupt_file_returns_none(self):
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertIsNone(TokenStore(self.path).load())

    def test_save_overwrites(self):
        store = TokenStore(self.path)
        store.save("tok-1", "s1")
        store.save("tok-2", "s2")
        self.assertEqual(store.load()["token"], "tok-2")


class ParseAuthorizeInputTest(unittest.TestCase):
    def test_full_url_with_code_in_query(self):
        kind, value = parse_authorize_input(
            "http://s.miwifi.com/dist/userhosts/index.html?gatewayIp=1.1.1.1&code=abc123"
        )
        self.assertEqual((kind, value), ("code", "abc123"))

    def test_full_url_with_token_in_fragment(self):
        kind, value = parse_authorize_input(
            "http://s.miwifi.com/dist/userhosts/index.html#access_token=tok-xyz&expires_in=7776000"
        )
        self.assertEqual((kind, value), ("token", "tok-xyz"))

    def test_bare_value(self):
        self.assertEqual(parse_authorize_input("raw-code-42"), ("code", "raw-code-42"))

    def test_empty_returns_none(self):
        self.assertIsNone(parse_authorize_input("   "))
        self.assertIsNone(parse_authorize_input(None))


class RunSyncWithRefreshTest(unittest.TestCase):
    def test_token_expired_calls_obtain_and_retries(self):
        client = mock.Mock()
        client.get_hosts.side_effect = [TokenExpiredError("expired"), ["1.1.1.1 a.com"]]
        obtain = mock.Mock()
        with mock.patch.object(main, "fetch_hosts", return_value="1.1.1.1 a.com\n"):
            run_sync_with_refresh(client, ["u"], 30, obtain)
        obtain.assert_called_once_with()
        self.assertEqual(client.get_hosts.call_count, 2)

    def test_success_no_obtain(self):
        client = mock.Mock()
        client.get_hosts.return_value = ["1.1.1.1 a.com"]
        obtain = mock.Mock()
        with mock.patch.object(main, "fetch_hosts", return_value="1.1.1.1 a.com\n"):
            run_sync_with_refresh(client, ["u"], 30, obtain)
        obtain.assert_not_called()


class AwaitAuthorizationTest(unittest.TestCase):
    def test_exchanges_code_and_caches_token(self):
        tmpdir = tempfile.mkdtemp()
        authorize_file = os.path.join(tmpdir, "authorize.url")
        with open(authorize_file, "w", encoding="utf-8") as fh:
            fh.write("http://s.miwifi.com/dist/userhosts/index.html?code=auth-code")
        try:
            client = mock.Mock()
            client.exchange_code.return_value = ("new-token", "1+1000+3")
            store = mock.Mock()
            with mock.patch.object(main.time, "sleep", lambda s: None):
                main.await_authorization(client, store, authorize_file, {"flag": False}, None)
            client.exchange_code.assert_called_once_with("auth-code", mock.ANY)
            store.save.assert_called_once_with("new-token", "1+1000+3")
            self.assertFalse(os.path.exists(authorize_file))
        finally:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_waits_until_file_appears(self):
        tmpdir = tempfile.mkdtemp()
        authorize_file = os.path.join(tmpdir, "authorize.url")
        try:
            client = mock.Mock()
            client.exchange_code.return_value = ("tok", "s")
            created = {"flag": False}

            def create_file():
                if not created["flag"]:
                    created["flag"] = True
                    with open(authorize_file, "w", encoding="utf-8") as fh:
                        fh.write("bare-code")

            with mock.patch.object(main.time, "sleep", lambda s: create_file()):
                main.await_authorization(client, mock.Mock(), authorize_file, {"flag": False}, None)
            client.exchange_code.assert_called_once()
        finally:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)


class SyncOnceTest(unittest.TestCase):
    def test_skips_write_when_unchanged(self):
        client = mock.Mock()
        client.get_hosts.return_value = parse_hosts("1.1.1.1 github.com\n")
        with mock.patch.object(main, "fetch_hosts", return_value="1.1.1.1 github.com\n") as fetch:
            changed, managed, manual = sync_once(client, ["u"], 30)
        self.assertFalse(changed)
        self.assertEqual(managed, 1)
        self.assertEqual(manual, 0)
        client.set_hosts.assert_not_called()

    def test_writes_merged_hosts(self):
        client = mock.Mock()
        client.get_hosts.return_value = ["9.9.9.9 my-router.local"]
        with mock.patch.object(main, "fetch_hosts", return_value="1.1.1.1 github.com\n"):
            changed, managed, manual = sync_once(client, ["u"], 30)
        self.assertTrue(changed)
        self.assertEqual(managed, 1)
        self.assertEqual(manual, 1)
        merged = client.set_hosts.call_args.args[0]
        self.assertEqual(merged, ["9.9.9.9 my-router.local", "1.1.1.1 github.com"])
        self.assertLessEqual(total_length(merged), main.MAX_HOSTS_LEN)

    def test_rejects_empty_source(self):
        client = mock.Mock()
        with mock.patch.object(main, "fetch_hosts", return_value="# only comments\n"):
            with self.assertRaises(RuntimeError):
                sync_once(client, ["u"], 30)
        client.get_hosts.assert_not_called()


if __name__ == "__main__":
    unittest.main()
