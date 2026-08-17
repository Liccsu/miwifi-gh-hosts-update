"""同步入口: 拉取 GitHub-IP-hosts 数据并写入小米路由器自定义 Hosts。

云端容器中常驻运行, 每次启动立即同步一次, 之后按 SYNC_INTERVAL_SECONDS
周期性执行。

token 管理 (持久部署), 按优先级:
1. 缓存文件 TOKEN_CACHE_FILE (默认 /data/token.json)
2. 环境变量 MIWIFI_TOKEN
3. 账号自动刷新: 配置 MIWIFI_XIAOMI_USER / MIWIFI_XIAOMI_PASS 时,
   通过 passport 登录 + OAuth 授权码自动换取新 token (适用于已信任设备)
4. 授权链接模式: 程序输出授权 URL, 用户浏览器打开登录小米账号
   (新设备首次需短信验证, 无法全自动), 把回跳 URL 写入 AUTHORIZE_FILE,
   程序自动换取并缓存 token。每次 90 天到期后重复该操作即可
"""

import argparse
import json
import logging
import os
import signal
import sys
import tempfile
import time
import urllib.parse
import urllib.request

from .gorouter import DEFAULT_APP_ID, DEFAULT_BASE_URL, DEFAULT_SCOPE, GorouterClient, TokenExpiredError
from .hostsfile import MAX_HOSTS_LEN, merge, parse_hosts, total_length
from .passport import LoginError, XiaomiAccount

logger = logging.getLogger("miwifi-hosts")

DEFAULT_HOSTS_URLS = [
    "https://raw.githubusercontent.com/ittuann/GitHub-IP-hosts/main/hosts",
    "https://cdn.jsdelivr.net/gh/ittuann/GitHub-IP-hosts@main/hosts",
    "https://fastly.jsdelivr.net/gh/ittuann/GitHub-IP-hosts@main/hosts",
]

DEFAULT_TOKEN_CACHE_FILE = "/data/token.json"
DEFAULT_AUTHORIZE_FILE = "/data/authorize.url"
# 90 天有效期, 提前 30 天主动刷新
DEFAULT_REFRESH_INTERVAL = 60 * 24 * 3600

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _env(name, default=None):
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def setup_logging(level):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


def fetch_hosts(urls, timeout):
    """依次尝试数据源 URL, 返回首个成功下载的文本。"""
    last_error = None
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8")
        except Exception as exc:  # 网络错误, 尝试下一个数据源
            last_error = exc
            logger.warning("下载失败 %s: %s", url, exc)
    raise RuntimeError(f"所有数据源均下载失败: {last_error}")


class TokenStore:
    """access_token 磁盘缓存, 原子写入。"""

    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        if not data.get("token"):
            return None
        return data

    def save(self, token, scope):
        data = {"token": token, "scope": scope, "issued_at": int(time.time())}
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix="token-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def build_redirect_uri(device_id):
    """构造与真实页面一致的 OAuth 回调地址。

    必须保持确定性: get_acc_token 会校验 redirect_uri 与授权时一致,
    因此不能包含每次变化的时间戳等参数。
    """
    gateway = _env("MIWIFI_GATEWAY_IP", "192.168.1.1")
    model = _env("MIWIFI_MODEL", "xiaomi.router.rd15")
    return (
        "http://s.miwifi.com/dist/userhosts/index.html"
        f"?gatewayIp={gateway}&language=zh&model={model}&deviceID={device_id}"
    )


def build_authorize_url(client, redirect_uri):
    """构造 OAuth 授权链接 (用户浏览器打开, 登录后回跳携带 code)。"""
    return (
        "https://account.xiaomi.com/oauth2/authorize"
        f"?client_id={client.app_id}"
        f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
        "&response_type=code&skip_confirm=true"
    )


def parse_authorize_input(text):
    """从用户粘贴的授权回跳内容中解析 (kind, value)。

    kind 为 "code" (需向 gorouter 换取 token) 或 "token" (直接可用)。
    支持完整 URL (query 中 code / fragment 中 access_token) 或裸值。
    """
    text = (text or "").strip()
    if not text:
        return None
    if "://" in text:
        split = urllib.parse.urlsplit(text)
        query = urllib.parse.parse_qs(split.query)
        if query.get("code"):
            return ("code", query["code"][0])
        fragment = urllib.parse.parse_qs(split.fragment)
        if fragment.get("access_token"):
            return ("token", fragment["access_token"][0])
    return ("code", text)


def make_client(token, scope):
    return GorouterClient(
        token=token,
        device_id=_env("MIWIFI_DEVICE_ID"),
        app_id=_env("MIWIFI_APP_ID", DEFAULT_APP_ID),
        scope=scope or _env("MIWIFI_SCOPE", DEFAULT_SCOPE),
        base_url=_env("GOROUTER_BASE_URL", DEFAULT_BASE_URL),
        timeout=int(_env("HTTP_TIMEOUT", "30")),
    )


def refresh_token(account, client, store):
    """账号自动刷新: passport 登录 -> OAuth 授权码 -> 换 token。"""
    redirect_uri = build_redirect_uri(client.device_id)
    token, scope = account.refresh_token(client.app_id, redirect_uri)
    client.token = token
    client.scope = scope or client.scope
    store.save(token, client.scope)
    logger.info("access_token 已通过账号自动刷新")
    return token


def await_authorization(client, store, authorize_file, stop):
    """授权链接模式: 输出链接, 轮询用户写入的授权文件直到成功。"""
    redirect_uri = build_redirect_uri(client.device_id)
    url = build_authorize_url(client, redirect_uri)
    logger.warning("=" * 64)
    logger.warning("需要授权: 请用浏览器打开以下链接, 登录小米账号完成授权")
    logger.warning("(新设备首次登录可能需短信/App 验证码确认, 属正常安全流程)")
    logger.warning("  %s", url)
    logger.warning("授权后浏览器会跳转到 s.miwifi.com, 复制地址栏完整 URL,")
    logger.warning("写入文件: %s", authorize_file)
    logger.warning("docker compose 场景: 写入宿主机挂载目录 ./data/authorize.url 即可")
    logger.warning("=" * 64)

    invalid_seen = set()
    while not stop["flag"]:
        try:
            with open(authorize_file, "r", encoding="utf-8") as fh:
                content = fh.read().strip()
        except OSError:
            content = None
        if content and content not in invalid_seen:
            parsed = parse_authorize_input(content)
            if parsed:
                kind, value = parsed
                try:
                    if kind == "token":
                        token, scope = value, None
                    else:
                        token, scope = client.exchange_code(value, redirect_uri)
                    client.token = token
                    client.scope = scope or client.scope
                    store.save(token, client.scope)
                    try:
                        os.unlink(authorize_file)
                    except OSError:
                        pass
                    logger.info("授权成功, access_token 已缓存 (有效期约 90 天)")
                    return
                except Exception as exc:
                    logger.error("授权内容无效: %s (code 约 10 分钟内有效, 请重新授权)", exc)
                    invalid_seen.add(content)
        time.sleep(30)
    logger.info("授权流程被中断")


def run_sync_with_refresh(client, urls, timeout, obtain):
    """同步一次; token 失效时调用 obtain 重新获取后重试。"""
    try:
        sync_once(client, urls, timeout)
    except TokenExpiredError:
        logger.warning("access_token 失效, 尝试重新获取")
        obtain()
        sync_once(client, urls, timeout)


def sync_once(client, urls, timeout):
    """执行一次完整同步, 返回是否发生写入。"""
    content = fetch_hosts(urls, timeout)
    managed = parse_hosts(content)
    if not managed:
        raise RuntimeError("数据源解析结果为空, 拒绝写入")

    existing = client.get_hosts()
    if not existing:
        logger.info("路由器当前自定义 hosts 为空")

    merged = merge(existing, managed)
    if merged == existing:
        logger.info("无变化, 跳过写入 (现有 %d 条)", len(existing))
        return False

    length = total_length(merged)
    if length > MAX_HOSTS_LEN:
        raise RuntimeError(
            f"合并后内容 {length} 字符超过上限 {MAX_HOSTS_LEN}, 跳过写入; "
            "请检查路由器上的手动条目是否过多"
        )

    client.set_hosts(merged)
    logger.info(
        "写入成功: 共 %d 条 (保留 %d 条手动条目 + %d 条托管条目, %d 字符)",
        len(merged),
        len(merged) - len(managed),
        len(managed),
        length,
    )
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description="同步 GitHub-IP-hosts 到小米路由器自定义 Hosts")
    parser.add_argument("--once", action="store_true", help="只执行一次同步后退出 (默认常驻循环)")
    args = parser.parse_args(argv)

    setup_logging(_env("LOG_LEVEL", "INFO"))

    device_id = _env("MIWIFI_DEVICE_ID")
    if not device_id:
        parser.error("必须设置环境变量 MIWIFI_DEVICE_ID")
    static_token = _env("MIWIFI_TOKEN")
    user = _env("MIWIFI_XIAOMI_USER")
    password = _env("MIWIFI_XIAOMI_PASS")

    store = TokenStore(_env("TOKEN_CACHE_FILE", DEFAULT_TOKEN_CACHE_FILE))
    authorize_file = _env("AUTHORIZE_FILE", DEFAULT_AUTHORIZE_FILE)
    refresh_interval = int(_env("TOKEN_REFRESH_INTERVAL", str(DEFAULT_REFRESH_INTERVAL)))

    account = XiaomiAccount(user, password) if user and password else None
    cached = store.load()

    stop = {"flag": False}

    def on_signal(signum, frame):
        logger.info("收到信号 %s, 准备退出", signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    # token 来源优先级: 缓存 > 环境变量 > 账号刷新 > 授权链接
    token = cached["token"] if cached else static_token
    scope = (cached or {}).get("scope")
    client = make_client(token or "", scope)

    def obtain():
        """重新获取 token: 优先账号刷新, 否则授权链接模式。"""
        if account:
            try:
                refresh_token(account, client, store)
                return
            except LoginError as exc:
                logger.error("账号自动登录失败: %s, 转入授权链接模式", exc)
        await_authorization(client, store, authorize_file, stop)

    if not token:
        logger.info("未找到可用 token, 开始获取")
        obtain()

    urls = [u.strip() for u in _env("HOSTS_URLS", ",".join(DEFAULT_HOSTS_URLS)).split(",") if u.strip()]
    interval = int(_env("SYNC_INTERVAL_SECONDS", "21600"))
    timeout = int(_env("HTTP_TIMEOUT", "30"))

    logger.info("启动: 数据源 %d 个, 同步间隔 %d 秒", len(urls), interval)
    while not stop["flag"]:
        started = time.monotonic()
        try:
            # 有账号时, token 到期前主动刷新 (缓存缺失时启动阶段已处理, 跳过)
            if account:
                cached_now = store.load()
                if cached_now and int(time.time()) - int(cached_now.get("issued_at", 0)) >= refresh_interval:
                    refresh_token(account, client, store)
            run_sync_with_refresh(client, urls, timeout, obtain)
        except LoginError as exc:
            logger.error("账号自动登录失败: %s", exc)
        except Exception as exc:
            logger.error("同步失败: %s", exc)
        if stop["flag"]:
            break
        if args.once:
            break
        elapsed = time.monotonic() - started
        time.sleep(max(interval - elapsed, 0))

    logger.info("退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
