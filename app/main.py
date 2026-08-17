"""同步入口: 拉取 GitHub-IP-hosts 数据并写入小米路由器自定义 Hosts。

云端容器中常驻运行, 每次启动立即同步一次, 之后按 SYNC_INTERVAL_SECONDS
周期性执行。

token 管理 (持久部署):
- 配置 MIWIFI_XIAOMI_USER / MIWIFI_XIAOMI_PASS 后, 程序通过小米账号自动
  刷新 access_token (passport 登录 -> OAuth 授权码 -> gorouter 换 token),
  新 token 缓存到 TOKEN_CACHE_FILE, 到期 (TOKEN_REFRESH_INTERVAL, 默认 60 天,
  早于 90 天有效期) 或失效 (HTTP 401 / code 3001) 时自动刷新, 无需人工干预
- 仅配置 MIWIFI_TOKEN 时保持静态 token 模式, 失效后输出 ERROR 日志等待人工更新
"""

import argparse
import json
import logging
import os
import signal
import sys
import tempfile
import time
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
    """构造与真实页面一致的 OAuth 回调地址。"""
    gateway = _env("MIWIFI_GATEWAY_IP", "192.168.1.1")
    model = _env("MIWIFI_MODEL", "xiaomi.router.rd15")
    return (
        "http://s.miwifi.com/dist/userhosts/index.html"
        f"?gatewayIp={gateway}&language=zh&model={model}"
        f"&deviceID={device_id}&t={int(time.time() * 1000)}"
    )


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
    """账号登录 -> OAuth 授权码 -> 换 token, 更新 client 并写缓存。"""
    redirect_uri = build_redirect_uri(client.device_id)
    token, scope = account.refresh_token(client.app_id, redirect_uri)
    client.token = token
    client.scope = scope or client.scope
    store.save(token, client.scope)
    logger.info("access_token 已通过账号自动刷新")
    return token


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


def run_sync(client, urls, timeout, account, store):
    """执行同步; token 失效且有账号时自动刷新并重试一次。"""
    try:
        sync_once(client, urls, timeout)
        return
    except TokenExpiredError:
        if not account:
            raise
        logger.info("token 失效, 尝试账号自动刷新")
        refresh_token(account, client, store)
        sync_once(client, urls, timeout)


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
    if not static_token and not (user and password):
        parser.error("必须设置 MIWIFI_TOKEN, 或提供 MIWIFI_XIAOMI_USER / MIWIFI_XIAOMI_PASS 以便自动获取")

    store = TokenStore(_env("TOKEN_CACHE_FILE", DEFAULT_TOKEN_CACHE_FILE))
    refresh_interval = int(_env("TOKEN_REFRESH_INTERVAL", str(DEFAULT_REFRESH_INTERVAL)))

    account = XiaomiAccount(user, password) if user and password else None
    cached = store.load()

    # token 来源优先级: 缓存 > 环境变量 > (有账号时) 自动刷新
    token = cached["token"] if cached else static_token
    scope = (cached or {}).get("scope")
    client = make_client(token or "", scope)

    if token:
        logger.info("使用 %s token", "缓存" if cached else "环境变量")
    if account and not token:
        refresh_token(account, client, store)

    urls = [u.strip() for u in _env("HOSTS_URLS", ",".join(DEFAULT_HOSTS_URLS)).split(",") if u.strip()]
    interval = int(_env("SYNC_INTERVAL_SECONDS", "21600"))
    timeout = int(_env("HTTP_TIMEOUT", "30"))

    stop = {"flag": False}

    def on_signal(signum, frame):
        logger.info("收到信号 %s, 准备退出", signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    logger.info("启动: 数据源 %d 个, 同步间隔 %d 秒", len(urls), interval)
    while not stop["flag"]:
        started = time.monotonic()
        try:
            # 有账号时, token 到期前主动刷新 (缓存缺失时启动阶段已处理, 跳过)
            if account:
                cached_now = store.load()
                if cached_now and int(time.time()) - int(cached_now.get("issued_at", 0)) >= refresh_interval:
                    refresh_token(account, client, store)
            run_sync(client, urls, timeout, account, store)
        except TokenExpiredError:
            logger.error(
                "access_token 失效且无可用账号凭据。请更新 MIWIFI_TOKEN 环境变量"
                "或配置 MIWIFI_XIAOMI_USER / MIWIFI_XIAOMI_PASS 后重启容器"
            )
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
