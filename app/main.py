"""同步入口: 拉取 GitHub-IP-hosts 数据并写入小米路由器自定义 Hosts。

云端容器中常驻运行, 每次启动立即同步一次, 之后按 SYNC_INTERVAL_SECONDS
周期性执行。token 失效 (HTTP 401 / code 3001) 时记录 ERROR 日志并持续
重试, 等待 token 被替换后自动恢复。
"""

import argparse
import logging
import os
import signal
import sys
import time
import urllib.request

from .gorouter import DEFAULT_APP_ID, DEFAULT_BASE_URL, DEFAULT_SCOPE, GorouterClient, TokenExpiredError
from .hostsfile import MAX_HOSTS_LEN, merge, parse_hosts, total_length

logger = logging.getLogger("miwifi-hosts")

DEFAULT_HOSTS_URLS = [
    "https://raw.githubusercontent.com/ittuann/GitHub-IP-hosts/main/hosts",
    "https://cdn.jsdelivr.net/gh/ittuann/GitHub-IP-hosts@main/hosts",
    "https://fastly.jsdelivr.net/gh/ittuann/GitHub-IP-hosts@main/hosts",
]

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

    token = _env("MIWIFI_TOKEN")
    device_id = _env("MIWIFI_DEVICE_ID")
    if not token or not device_id:
        parser.error("必须设置环境变量 MIWIFI_TOKEN 与 MIWIFI_DEVICE_ID")

    client = GorouterClient(
        token=token,
        device_id=device_id,
        app_id=_env("MIWIFI_APP_ID", DEFAULT_APP_ID),
        scope=_env("MIWIFI_SCOPE", DEFAULT_SCOPE),
        base_url=_env("GOROUTER_BASE_URL", DEFAULT_BASE_URL),
        timeout=int(_env("HTTP_TIMEOUT", "30")),
    )
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
            sync_once(client, urls, timeout)
        except TokenExpiredError as exc:
            logger.error(
                "access_token 已失效: %s。请重新打开小米路由器自定义 Hosts 页面 "
                "(http://s.miwifi.com/dist/userhosts/index.html?gatewayIp=%s), "
                "将地址栏 URL 中的 access_token 更新到 MIWIFI_TOKEN 环境变量, 然后重启容器",
                exc,
                "192.168.31.1",
            )
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
