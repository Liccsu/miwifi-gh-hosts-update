"""main 模块单元测试: token 缓存与失效自动刷新。"""

import json
import os
import tempfile
import unittest
from unittest import mock

from app import main
from app.gorouter import TokenExpiredError
from app.hostsfile import merge, parse_hosts, total_length
from app.main import TokenStore, run_sync, sync_once


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


class RunSyncTest(unittest.TestCase):
    def test_token_expired_refreshes_and_retries(self):
        client = mock.Mock()
        # 首次 get_hosts 抛 token 失效, 刷新后重试成功
        client.get_hosts.side_effect = [TokenExpiredError("expired"), ["1.1.1.1 a.com"]]
        account = mock.Mock()
        store = mock.Mock()
        with mock.patch.object(main, "fetch_hosts", return_value="1.1.1.1 a.com\n"), mock.patch.object(
            main, "refresh_token"
        ) as refresh:
            run_sync(client, ["u"], 30, account, store)
            refresh.assert_called_once_with(account, client, store)
            self.assertEqual(client.get_hosts.call_count, 2)

    def test_token_expired_without_account_raises(self):
        client = mock.Mock()
        client.get_hosts.side_effect = TokenExpiredError("expired")
        with mock.patch.object(main, "fetch_hosts", return_value="1.1.1.1 a.com\n"):
            with self.assertRaises(TokenExpiredError):
                run_sync(client, ["u"], 30, None, mock.Mock())

    def test_success_no_refresh(self):
        client = mock.Mock()
        client.get_hosts.return_value = ["1.1.1.1 a.com"]
        with mock.patch.object(main, "fetch_hosts", return_value="1.1.1.1 a.com\n"), mock.patch.object(
            main, "refresh_token"
        ) as refresh:
            run_sync(client, ["u"], 30, mock.Mock(), mock.Mock())
            refresh.assert_not_called()


class SyncOnceTest(unittest.TestCase):
    def test_skips_write_when_unchanged(self):
        client = mock.Mock()
        client.get_hosts.return_value = parse_hosts("1.1.1.1 github.com\n")
        with mock.patch.object(main, "fetch_hosts", return_value="1.1.1.1 github.com\n") as fetch:
            changed = sync_once(client, ["u"], 30)
        self.assertFalse(changed)
        client.set_hosts.assert_not_called()

    def test_writes_merged_hosts(self):
        client = mock.Mock()
        client.get_hosts.return_value = ["9.9.9.9 my-router.local"]
        with mock.patch.object(main, "fetch_hosts", return_value="1.1.1.1 github.com\n"):
            changed = sync_once(client, ["u"], 30)
        self.assertTrue(changed)
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
