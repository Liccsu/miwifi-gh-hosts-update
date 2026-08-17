"""passport 模块单元测试。"""

import unittest
from unittest import mock

from app.passport import (
    LoginError,
    VerifyRequired,
    XiaomiAccount,
    password_hash_v1,
    password_hash_v2,
)


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

    def test_login_success_without_verification(self):
        self.acc._passport_json.side_effect = [
            self._sid_ok(),
            {"code": 0, "userId": "123", "securityStatus": 0,
             "location": "https://account.xiaomi.com/finish"},
        ]
        result = self.acc.login()
        self.assertEqual(result["status"], "ok")
        self.acc._open.assert_called_once_with("https://account.xiaomi.com/finish")

    def test_login_verify_required_extracts_context(self):
        self.acc._passport_json.side_effect = [
            self._sid_ok(),
            {"code": 0, "securityStatus": 16,
             "notificationUrl": "https://account.xiaomi.com/fe/service/identity/authStart?sid=xiaomiio&context=CTX123"},
        ]
        result = self.acc.login()
        self.assertEqual(result["status"], "verify_required")
        self.assertEqual(result["context"], "CTX123")
        self.acc._open.assert_not_called()

    def test_login_captcha_raises(self):
        self.acc._passport_json.side_effect = [
            self._sid_ok(),
            {"code": 87001, "description": "captcha required"},
        ]
        with self.assertRaises(LoginError) as ctx:
            self.acc.login()
        self.assertIn("图形验证码", str(ctx.exception))

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


class VerificationFlowTest(unittest.TestCase):
    def setUp(self):
        self.acc = XiaomiAccount("user@example.com", "secret")
        self.acc._passport_json = mock.Mock()
        self.acc._open = mock.Mock()

    def test_send_verification_code(self):
        self.acc._passport_json.side_effect = [
            {"code": 2, "flag": 4, "options": [4]},  # identity/list
            {"code": 0},  # userQuota
            {"code": 0, "maskedPhone": "+86 199****41"},  # sendPhoneTicket
        ]
        flag, masked = self.acc.send_verification_code("CTX1")
        self.assertEqual(flag, 4)
        self.assertEqual(masked, "+86 199****41")

    def test_send_verification_no_options_raises(self):
        self.acc._passport_json.side_effect = [{"code": 2, "options": []}]
        with self.assertRaises(LoginError):
            self.acc.send_verification_code("CTX1")

    def test_send_verification_failure_raises(self):
        self.acc._passport_json.side_effect = [
            {"code": 2, "flag": 4, "options": [4]},
            {"code": 0},
            {"code": -1, "description": "blocked"},
        ]
        with self.assertRaises(LoginError):
            self.acc.send_verification_code("CTX1")

    def test_submit_verification_code_success(self):
        self.acc._passport_json.side_effect = [
            {"code": 0, "description": "成功"},  # verifyPhone
        ]
        self.acc.submit_verification_code("CTX1", 4, "123456")
        # verifyPhone 请求体包含验证码
        args, _ = self.acc._passport_json.call_args_list[0]
        body = args[1].decode()
        self.assertIn("ticket=123456", body)
        self.assertIn("_flag=4", body)
        # 不再跟随完成链 (end 需要 _signature, 改为重新登录)
        self.acc._open.assert_not_called()

    def test_submit_verification_wrong_code_raises(self):
        self.acc._passport_json.side_effect = [{"code": -1, "description": "code wrong"}]
        with self.assertRaises(LoginError):
            self.acc.submit_verification_code("CTX1", 4, "000000")


class AccessTokenTest(unittest.TestCase):
    def setUp(self):
        self.acc = XiaomiAccount("user@example.com", "secret")
        self.acc._open = mock.Mock()

    def test_get_access_token_parses_fragment(self):
        resp = mock.Mock()
        resp.geturl.return_value = (
            "https://s.miwifi.com/dist/userhosts/index.html"
            "#access_token=V3_TOK123&mac_key=k&scope=1+1000+3&expires_in=7776000"
        )
        self.acc._open.return_value = resp
        token, scope = self.acc.get_access_token("app-id", "https://redirect")
        self.assertEqual(token, "V3_TOK123")
        self.assertEqual(scope, "1 1000 3")  # fragment 中 + 解码为空格
        url = self.acc._open.call_args.args[0]
        self.assertIn("response_type=token", url)

    def test_get_access_token_missing_raises(self):
        resp = mock.Mock()
        resp.geturl.return_value = "https://s.miwifi.com/dist/userhosts/index.html#error=denied"
        self.acc._open.return_value = resp
        with self.assertRaises(LoginError):
            self.acc.get_access_token("app-id", "https://redirect")

    def test_refresh_token_raises_verify_required(self):
        with mock.patch.object(self.acc, "login", return_value={"status": "verify_required", "context": "C"}):
            with self.assertRaises(VerifyRequired):
                self.acc.refresh_token("app-id", "https://redirect")


if __name__ == "__main__":
    unittest.main()
