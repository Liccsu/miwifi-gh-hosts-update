"""小米路由器自定义 Hosts 云端接口客户端。

接口经由小米云服务 gorouter.info 转发, 需要小米账号授权的 access_token。
鉴权方式: token 作为普通请求参数传递 (逆向自 s.miwifi.com 前端
router_request_3.js 与 userhosts/index.js)。
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

PATH_CUSTOM_HOST_GET = "/api-third-party/service/internal/custom_host_get"
PATH_CUSTOM_HOST_SET = "/api-third-party/service/internal/custom_host_set"

DEFAULT_APP_ID = "2882303761517675329"
DEFAULT_SCOPE = "1+1000+3"
DEFAULT_BASE_URL = "https://www.gorouter.info"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class TokenExpiredError(RuntimeError):
    """access_token 失效 (HTTP 401 或业务码 3001), 需要重新授权。"""


class ApiError(RuntimeError):
    """接口返回非零业务码。"""


class GorouterClient:
    def __init__(
        self,
        *,
        token,
        device_id,
        app_id=DEFAULT_APP_ID,
        scope=DEFAULT_SCOPE,
        base_url=DEFAULT_BASE_URL,
        timeout=30,
    ):
        self.token = token
        self.device_id = device_id
        self.app_id = app_id
        self.scope = scope
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _common_params(self):
        return {
            "appId": self.app_id,
            "deviceId": self.device_id,
            "clientId": self.app_id,
            "scope": self.scope,
            "token": self.token,
        }

    def _open(self, url, data=None):
        headers = {"User-Agent": _UA}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise TokenExpiredError("HTTP 401, access_token 已失效") from exc
            raise ApiError(f"HTTP {exc.code}") from exc

    def _check(self, data):
        code = data.get("code")
        if code == 0:
            return data.get("hosts", [])
        if code == 3001:
            raise TokenExpiredError(f"code=3001: {data.get('msg', 'token 已失效')}")
        raise ApiError(f"code={code}: {data.get('msg', '未知错误')}")

    def get_hosts(self):
        """读取路由器当前自定义 hosts, 返回条目列表。"""
        url = self.base_url + PATH_CUSTOM_HOST_GET + "?" + urllib.parse.urlencode(self._common_params())
        logger.debug("GET %s", url)
        return self._check(self._open(url))

    def set_hosts(self, entries):
        """全量覆盖写入自定义 hosts。"""
        params = self._common_params()
        params["hosts"] = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
        body = urllib.parse.urlencode(params).encode("utf-8")
        url = self.base_url + PATH_CUSTOM_HOST_SET
        logger.debug("POST %s (%d entries)", url, len(entries))
        return self._check(self._open(url, body))
