"""小米账号 passport 登录与 OAuth 授权码获取, 用于自动刷新 access_token。

流程 (与 Xiaomi-cloud-tokens-extractor / python-miio 等社区实现一致):
1. GET  https://account.xiaomi.com/pass/sid            取 _sign
2. POST https://account.xiaomi.com/pass/serviceLoginAuth2
   提交账号与密码摘要, 响应中带 passToken 等会话 cookie 与跳转链
3. 访问跳转链, 完成登录态
4. GET  https://account.xiaomi.com/oauth2/authorize
   带会话 cookie, skip_confirm=true, 已登录用户被 302 回 redirect_uri 并携带 code
5. GET  https://www.gorouter.info/oauth/get_acc_token
   用一次性 code 换取 access_token (小米内部服务, 无需 client_secret)

限制: 账号开启二次验证/风控要求验证码时 (code 87001) 无法自动登录,
会抛出 LoginError, 由调用方回退到静态 token 并告警。
"""

import hashlib
import http.cookiejar
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

PASSPORT_BASE = "https://account.xiaomi.com"
GOROUTER_BASE = "https://www.gorouter.info"

SID = "xiaomiio"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

CODE_CAPTCHA = 87001  # 需要验证码
CODE_PASSWORD = 70016  # 密码错误


class LoginError(RuntimeError):
    """登录失败 (密码错误 / 验证码 / 风控), 无法自动完成。"""


def password_hash_v1(password):
    """旧式摘要: MD5(密码) 大写 (Xiaomi-cloud-tokens-extractor 风格)。"""
    return hashlib.md5(password.encode("utf-8")).hexdigest().upper()


def password_hash_v2(user, password):
    """新式摘要: MD5(user + MD5(密码) 小写) (python-miio 风格)。"""
    inner = hashlib.md5(password.encode("utf-8")).hexdigest()
    return hashlib.md5((user + inner).encode("utf-8")).hexdigest()


class XiaomiAccount:
    def __init__(self, user, password, timeout=30, base_url=PASSPORT_BASE):
        self.user = user
        self.password = password
        self.timeout = timeout
        self.base_url = base_url.rstrip("/")
        self._cookie_jar = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cookie_jar)
        )

    def _open(self, url, data=None, headers=None):
        req_headers = {"User-Agent": _UA}
        if data is not None:
            req_headers["Content-Type"] = "application/x-www-form-urlencoded"
        req_headers.update(headers or {})
        return self._opener.open(
            urllib.request.Request(url, data=data, headers=req_headers),
            timeout=self.timeout,
        )

    def _passport_json(self, url, data=None):
        """请求 passport 接口并解析 JSON (响应带 &&&START&&& JSONP 前缀)。"""
        with self._open(url, data, {"Accept": "application/json"}) as resp:
            body = resp.read().decode("utf-8")
        body = body.split("&&&START&&&", 1)[-1].strip()
        return json.loads(body)

    def login(self):
        """执行 passport 登录, 建立会话 cookie。失败抛 LoginError。"""
        sign = self._fetch_sign()
        logged = self._submit_credentials(sign)
        # 跟随跳转链完成登录态 (location 指向带 serviceToken 的页面)
        self._open(logged["location"])
        return logged

    def _fetch_sign(self):
        url = f"{self.base_url}/pass/serviceLogin?sid={SID}&_json=true"
        data = self._passport_json(url)
        sign = data.get("_sign")
        if not sign:
            raise LoginError(f"pass/serviceLogin 失败: code={data.get('code')} {data.get('description', '')}")
        return sign

    def _submit_credentials(self, sign):
        # 两种摘要风格先后尝试, 兼容不同账号体系
        hashes = (
            ("v1", password_hash_v1(self.password)),
            ("v2", password_hash_v2(self.user, self.password)),
        )
        for attempt, pwd_hash in hashes:
            fields = {
                "sid": SID,
                "hash": pwd_hash,
                "callback": "https://sts.api.io.mi.com/sts",
                "qs": urllib.parse.quote(f"?sid={SID}&_json=true", safe=""),
                "user": self.user,
                "_sign": sign,
                "_json": "true",
            }
            body = urllib.parse.urlencode(fields).encode("utf-8")
            try:
                data = self._passport_json(f"{self.base_url}/pass/serviceLoginAuth2", body)
            except (urllib.error.HTTPError, ValueError) as exc:
                if attempt == "v2":
                    raise LoginError(f"登录请求被拒绝: {exc}") from exc
                continue
            code = data.get("code")
            if code == 0:
                logger.info("passport 登录成功 (hash=%s), userId=%s", attempt, data.get("userId"))
                return data
            if code == CODE_CAPTCHA:
                raise LoginError("登录需要验证码 (code 87001), 无法自动登录")
            if code == CODE_PASSWORD and attempt == "v2":
                raise LoginError("账号或密码错误 (code 70016)")
            if attempt == "v2":
                raise LoginError(f"登录失败: code={code} {data.get('description', '')}")
        raise LoginError("登录失败: 两种密码摘要均未成功")

    def get_auth_code(self, client_id, redirect_uri):
        """对已登录会话发起 OAuth 授权, 返回 302 回跳 URL 中的 code。"""
        url = (
            f"{self.base_url}/oauth2/authorize"
            f"?client_id={client_id}"
            f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
            "&response_type=code&skip_confirm=true"
        )
        try:
            resp = self._open(url)
        except urllib.error.HTTPError as exc:
            raise LoginError(f"OAuth 授权失败: HTTP {exc.code}") from exc
        final = resp.geturl()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(final).query)
        code = query.get("code")
        if not code:
            raise LoginError(f"OAuth 授权未返回 code, 最终地址: {final[:200]}")
        logger.debug("OAuth 授权码获取成功")
        return code[0]

    def exchange_token(self, code, client_id, redirect_uri):
        """用授权码向 gorouter 换取 access_token, 返回 (token, scope)。"""
        url = (
            f"{GOROUTER_BASE}/oauth/get_acc_token"
            f"?code={urllib.parse.quote(code, safe='')}"
            f"&clientId={client_id}"
            f"&redirectUri={urllib.parse.quote(redirect_uri, safe='')}"
        )
        data = self._passport_json(url)
        if data.get("code") != 0 or not data.get("data"):
            raise LoginError(f"换取 token 失败: {json.dumps(data, ensure_ascii=False)[:200]}")
        payload = data["data"]
        return payload["access_token"], payload.get("scope")

    def refresh_token(self, client_id, redirect_uri):
        """完整刷新流程: 登录 -> 授权码 -> access_token。"""
        self.login()
        code = self.get_auth_code(client_id, redirect_uri)
        return self.exchange_token(code, client_id, redirect_uri)
