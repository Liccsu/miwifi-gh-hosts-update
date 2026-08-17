"""passport 模块单元测试。"""

import unittest
from unittest import mock

from app.passport import LoginError, XiaomiAccount, password_hash_v1, password_hash_v2


class PasswordHashTest(unittest.TestCase):
    def test_v1_uppercase_md5(self):
        self.assertEqual(password_hash_v1("password"), "5F4DCC3B5AA765D61D8327DEB882CF99")

    def test_v2_nested_md5(self):
        self.assertEqual(
            password_hash_v2("user", "password"), "f8030584958fba84e562bbaa4bcf204e"
        )


class LoginFlowTest(unittest.TestCase):
    def setUp(self):
        self.acc = XiaomiAccount("user@example.com", "secret")
        self.acc._passport_json = mock.Mock()
        self.acc._open = mock.Mock()

    def _sid_ok(self, sign="s3cret-sign"):
        return {"_sign": sign}

    def test_login_success(self):
        self.acc._passport_json.side_effect = [
            self._sid_ok(),
            {"code": 0, "userId": "123", "location": "https://account.xiaomi.com/finish"},
        ]
        result = self.acc.login()
        self.assertEqual(result["userId"], "123")
        self.acc._open.assert_called_once_with("https://account.xiaomi.com/finish")
        # 提交的字段包含账号与摘要
        args, _ = self.acc._passport_json.call_args_list[1]
        body = args[1].decode()
        self.assertIn("user=user%40example.com", body)
        self.assertIn("_sign=s3cret-sign", body)

    def test_login_captcha_raises(self):
        self.acc._passport_json.side_effect = [
            self._sid_ok(),
            {"code": 87001, "description": "captcha required"},
        ]
        with self.assertRaises(LoginError) as ctx:
            self.acc.login()
        self.assertIn("验证码", str(ctx.exception))

    def test_login_wrong_password_raises(self):
        self.acc._passport_json.side_effect = [
            self._sid_ok(),
            {"code": 70016, "description": "password wrong"},  # v1 摘要失败
            {"code": 70016, "description": "password wrong"},  # v2 摘要也失败
        ]
        with self.assertRaises(LoginError) as ctx:
            self.acc.login()
        self.assertIn("账号或密码错误", str(ctx.exception))

    def test_sid_failure_raises(self):
        self.acc._passport_json.side_effect = [{"code": -1, "description": "bad"}]
        with self.assertRaises(LoginError):
            self.acc.login()

    def test_get_auth_code_parses_302_target(self):
        resp = mock.Mock()
        resp.geturl.return_value = (
            "http://s.miwifi.com/dist/userhosts/index.html?gatewayIp=192.168.1.1&code=auth-code-42"
        )
        self.acc._open.return_value = resp
        code = self.acc.get_auth_code("app-id", "http://s.miwifi.com/dist/userhosts/index.html")
        self.assertEqual(code, "auth-code-42")
        url = self.acc._open.call_args.args[0]
        self.assertIn("client_id=app-id", url)
        self.assertIn("response_type=code", url)
        self.assertIn("skip_confirm=true", url)

    def test_get_auth_code_missing_code_raises(self):
        resp = mock.Mock()
        resp.geturl.return_value = "http://s.miwifi.com/dist/userhosts/index.html?error=denied"
        self.acc._open.return_value = resp
        with self.assertRaises(LoginError):
            self.acc.get_auth_code("app-id", "http://s.miwifi.com/dist/userhosts/index.html")

    def test_exchange_token(self):
        self.acc._passport_json.return_value = {
            "code": 0,
            "data": {"access_token": "tok-1", "scope": "1+1000+3"},
        }
        token, scope = self.acc.exchange_token("c0de", "app-id", "http://redirect")
        self.assertEqual((token, scope), ("tok-1", "1+1000+3"))

    def test_exchange_token_failure_raises(self):
        self.acc._passport_json.return_value = {"code": -1, "msg": "bad code"}
        with self.assertRaises(LoginError):
            self.acc.exchange_token("bad", "app-id", "http://redirect")


if __name__ == "__main__":
    unittest.main()
