"""小米账号 passport 登录与 access_token 自动刷新。

流程 (经真实逆向验证):
1. GET  https://account.xiaomi.com/pass/serviceLogin      取 _sign
2. POST https://account.xiaomi.com/pass/serviceLoginAuth2
   提交账号与密码摘要。响应 securityStatus=16 时触发设备安全验证:
   - GET  /identity/list           确认验证方式 (安全手机/邮箱)
   - POST /identity/pass/sms/userQuota           配额检查
   - POST /identity/auth/sendPhoneTicket         发送验证码
   - POST /identity/auth/verifyPhone             提交验证码 (人工输入)
   - 跟随 /identity/result/check -> /pass/serviceLoginAuth2/end 完成登录态
3. GET  https://account.xiaomi.com/oauth2/authorize?response_type=token
   带会话 cookie 直接获取 access_token (implicit flow, 有效期 90 天)

限制: 新设备/异地环境登录必然触发安全验证 (securityStatus=16), 验证码
需用户人工输入; 账号配置了密码即可, 无需二次验证码以外的交互。
"""

import hashlib
import http.cookiejar
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

PASSPORT_BASE = "https://account.xiaomi.com"

SID = "xiaomiio"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

CODE_CAPTCHA = 87001  # 登录表单验证码
CODE_PASSWORD = 70016  # 密码错误
SECURITY_STATUS_VERIFY = 16  # 设备安全验证


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

    # ---------- 登录 ----------

    def login(self):
        """密码登录。

        返回 {"status": "ok", "data": {...}} 或
             {"status": "verify_required", "context": str, "data": {...}}
        失败抛 LoginError。
        """
        sign = self._fetch_sign()
        logged = self._submit_credentials(sign)
        if logged.get("securityStatus") != SECURITY_STATUS_VERIFY:
            if logged.get("location"):
                self._open(logged["location"])
            logger.info("passport 登录成功, 无需安全验证")
            return {"status": "ok", "data": logged}
        notification_url = logged.get("notificationUrl") or ""
        context = urllib.parse.parse_qs(
            urllib.parse.urlsplit(notification_url).query
        ).get("context", [""])[0]
        if not context:
            raise LoginError("安全验证缺少 context, 登录失败")
        logger.info("登录需设备安全验证 (securityStatus=%s)", SECURITY_STATUS_VERIFY)
        return {"status": "verify_required", "context": context, "data": logged}

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
                logger.info("密码校验通过 (hash=%s), userId=%s", attempt, data.get("userId"))
                return data
            if code == CODE_CAPTCHA:
                raise LoginError("登录需要图形验证码 (code 87001), 无法自动登录")
            if code == CODE_PASSWORD and attempt == "v2":
                raise LoginError("账号或密码错误 (code 70016)")
            if attempt == "v2":
                raise LoginError(f"登录失败: code={code} {data.get('description', '')}")
        raise LoginError("登录失败: 两种密码摘要均未成功")

    # ---------- 设备安全验证 ----------

    def list_verification(self, context):
        """查询可用验证方式, 返回 identity/list 原始响应。"""
        url = (
            f"{self.base_url}/identity/list?sid={SID}&supportedMask=0"
            f"&_locale=zh_CN&context={urllib.parse.quote(context, safe='')}"
        )
        return self._passport_json(url)

    def send_verification_code(self, context):
        """发送验证码到安全手机, 返回 (验证方式 flag, 掩码手机)。"""
        lst = self.list_verification(context)
        options = lst.get("options") or []
        if not options:
            raise LoginError(f"账号无可用安全验证方式: {json.dumps(lst, ensure_ascii=False)}")
        flag = lst.get("flag") or options[0]
        # 配额检查 (失败不阻塞)
        try:
            self._passport_json(
                f"{self.base_url}/identity/pass/sms/userQuota",
                b"addressType=PH&contentType=160040&_json=true",
            )
        except Exception:
            pass
        # 发送验证码 (滑块码 icode 留空, 未触发滑块时有效)
        resp = self._passport_json(
            f"{self.base_url}/identity/auth/sendPhoneTicket",
            b"retry=0&icode=&_json=true",
        )
        if resp.get("code") != 0:
            raise LoginError(f"发送验证码失败: {json.dumps(resp, ensure_ascii=False)}")
        masked = None
        for field in ("maskedPhone", "maskedEmail", "address"):
            if resp.get(field):
                masked = resp[field]
                break
        return flag, masked

    def submit_verification_code(self, context, flag, ticket):
        """提交验证码; 成功后会话即具备已验证身份 (identity_session)。

        不再跟随 result/check -> end 链 (end 需要页面计算的 _signature,
        无法程序化构造); 改为由调用方在验证通过后重新执行 login(),
        已验证会话可绕过安全验证直接完成登录。
        """
        body = (
            f"_flag={flag}&ticket={urllib.parse.quote(ticket, safe='')}"
            "&trust=false&_json=true"
        ).encode("utf-8")
        resp = self._passport_json(f"{self.base_url}/identity/auth/verifyPhone", body)
        logger.debug("verifyPhone 响应: %s", json.dumps(resp, ensure_ascii=False)[:600])
        if resp.get("code") != 0:
            raise LoginError(
                f"验证码校验失败: {json.dumps(resp, ensure_ascii=False)}"
            )
        logger.info("验证码校验通过, 会话已具备已验证身份")
        # 服务端直接返回 result/check 跳转地址 (含 identityToken + _sign),
        # 无需客户端签名; 跟随该链完成登录态 (result/check -> end -> sts)
        check_url = resp.get("location")
        if check_url:
            self._follow_verification_chain(check_url)
        else:
            logger.warning("verifyPhone 响应缺少 location, 登录态可能不完整")

    def _follow_verification_chain(self, check_url):
        """跟随验证完成链: result/check -> serviceLoginAuth2/end -> sts。

        各跳转地址由服务端在响应中签发, 程序仅逐级跟随。
        """
        url = check_url
        for step in range(4):
            if not url:
                break
            try:
                resp = self._open(url)
                final = resp.geturl()
                logger.debug("完成链 step%d: %s -> %s", step, url[:90], final[:110])
            except urllib.error.HTTPError as exc:
                final = exc.headers.get("Location", "")
                logger.debug("完成链 step%d: %s -> HTTP %s Location=%s",
                             step, url[:90], exc.code, final[:110])
            url = final
        logger.debug(
            "登录态 cookies: %s", sorted(c.name for c in self._cookie_jar)
        )

    # ---------- access_token ----------

    def get_access_token(self, client_id, redirect_uri):
        """v1 implicit flow: 带会话 cookie 直取 access_token, 返回 (token, scope)。

        已登录会话访问 authorize?response_type=token 即被 302 回
        redirect_uri, fragment 携带 access_token / mac_key / scope 等。
        """
        url = (
            f"{self.base_url}/oauth2/authorize"
            f"?client_id={client_id}"
            f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
            "&response_type=token"
        )
        resp = self._open(url)
        final = resp.geturl()
        fragment = urllib.parse.urlsplit(final).fragment
        params = urllib.parse.parse_qs(fragment)
        token = params.get("access_token")
        if not token:
            raise LoginError(f"OAuth 未返回 access_token, 最终地址: {final[:200]}")
        return token[0], (params.get("scope") or [""])[0]

    def refresh_token(self, client_id, redirect_uri):
        """完整刷新: 密码登录 -> (安全验证) -> access_token。

        需要安全验证时抛 VerifyRequired, 由调用方完成验证码交互。
        """
        result = self.login()
        if result["status"] == "verify_required":
            raise VerifyRequired(result["context"])
        return self.get_access_token(client_id, redirect_uri)


class VerifyRequired(LoginError):
    """登录需设备安全验证, 需要用户输入短信验证码。"""

    def __init__(self, context):
        super().__init__("需要安全验证")
        self.context = context
