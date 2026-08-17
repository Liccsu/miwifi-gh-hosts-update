#!/usr/bin/env python3
"""小米账号安全验证状态探测脚本。

用途: 检测验证码发送风控/配额状态, 判断"验证码自动续期"功能何时可用。

安全设计:
- 默认只做只读探测 (登录 + identity/list + userQuota), 不发送短信
- --send 时才尝试发送验证码 (会向安全手机发一条真实短信)
- 每次运行只登录一次; 不要高频运行 (登录本身计入风控)

用法:
  python scripts/probe.py                 # 单次只读探测
  python scripts/probe.py --send          # 探测并尝试发送验证码 (发短信)
  python scripts/probe.py --interval 600  # 每 10 分钟循环探测 (只读)
"""

import argparse
import hashlib
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, "")  # 允许从项目根导入 app 包

from app.passport import password_hash_v1  # noqa: E402

SID = "xiaomiio"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def load_env(path=".env"):
    vals = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                vals[key.strip()] = value.strip()
    except OSError:
        pass
    return vals


def strip_json(body):
    return json.loads(body.split("&&&START&&&", 1)[-1].strip())


def probe(user, password, send=False):
    """执行一次探测, 返回状态字典。"""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def get(url, data=None):
        headers = {"User-Agent": UA}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        return opener.open(
            urllib.request.Request(url, data=data, headers=headers), timeout=30
        )

    status = {"time": time.strftime("%Y-%m-%d %H:%M:%S")}

    # 1) 登录取 sign + context
    try:
        with get(f"https://account.xiaomi.com/pass/serviceLogin?sid={SID}&_json=true") as r:
            sign = strip_json(r.read().decode())["_sign"]
    except Exception as exc:
        return {**status, "phase": "login-failed", "error": str(exc)[:120]}

    fields = {
        "sid": SID,
        "hash": password_hash_v1(password),
        "callback": "https://sts.api.io.mi.com/sts",
        "qs": urllib.parse.quote(f"?sid={SID}&_json=true", safe=""),
        "user": user,
        "_sign": sign,
        "_json": "true",
    }
    try:
        with get(
            "https://account.xiaomi.com/pass/serviceLoginAuth2",
            urllib.parse.urlencode(fields).encode(),
        ) as r:
            login = strip_json(r.read().decode())
    except Exception as exc:
        return {**status, "phase": "login-failed", "error": str(exc)[:120]}

    if login.get("code") != 0:
        return {
            **status,
            "phase": "login-rejected",
            "login_code": login.get("code"),
            "desc": login.get("description"),
        }
    status["security_status"] = login.get("securityStatus")

    if login.get("securityStatus") != 16:
        status["phase"] = "no-verification-needed"
        status["note"] = "当前环境登录无需安全验证 (已验证设备), 验证码自动续期可直接工作"
        return status

    # 2) 安全验证流程 (identity/list + userQuota)
    ctx = urllib.parse.parse_qs(
        urllib.parse.urlsplit(login.get("notificationUrl") or "").query
    ).get("context", [""])[0]
    if not ctx:
        return {**status, "phase": "no-context", "error": "登录响应缺少 context"}

    try:
        with get(
            f"https://account.xiaomi.com/identity/list?sid={SID}&supportedMask=0"
            f"&_locale=zh_CN&context={urllib.parse.quote(ctx, safe='')}"
        ) as r:
            lst = strip_json(r.read().decode())
        status["verify_options"] = lst.get("options")
    except urllib.error.HTTPError as exc:
        status["identity_list"] = f"HTTP {exc.code}"
    except Exception as exc:
        status["identity_list"] = str(exc)[:80]

    try:
        with get(
            "https://account.xiaomi.com/identity/pass/sms/userQuota",
            b"addressType=PH&contentType=160040&_json=true",
        ) as r:
            quota = strip_json(r.read().decode())
        status["quota_code"] = quota.get("code")
        status["quota_remaining"] = quota.get("info")
    except urllib.error.HTTPError as exc:
        status["quota_code"] = f"HTTP {exc.code}"
    except Exception as exc:
        status["quota_code"] = str(exc)[:80]

    # 3) 可选: 尝试发送验证码
    if send:
        try:
            with get(
                "https://account.xiaomi.com/identity/auth/sendPhoneTicket",
                b"retry=0&icode=&_json=true",
            ) as r:
                sent = strip_json(r.read().decode())
            status["send_code"] = sent.get("code")
            status["send_desc"] = sent.get("desc") or sent.get("description")
        except urllib.error.HTTPError as exc:
            status["send_code"] = f"HTTP {exc.code}"
        except Exception as exc:
            status["send_code"] = str(exc)[:80]

    # 状态归类
    if status.get("quota_code") == 10001:
        status["phase"] = "rate-limited"
        status["verdict"] = "风控中: 验证接口被限流, 需等待恢复 (勿频繁探测)"
    elif status.get("quota_code") == 0 and status.get("quota_remaining") == "0":
        status["phase"] = "quota-exhausted"
        status["verdict"] = "验证码配额已用完 (通常每日重置), 明日可再试"
    elif status.get("quota_code") == 0 and int(status.get("quota_remaining", 0) or 0) > 0:
        status["phase"] = "ready"
        status["verdict"] = "可发送验证码: 配额剩余 " + str(status["quota_remaining"]) + " 次"
    else:
        status["phase"] = "unknown"
        status["verdict"] = "状态未知, 请结合上方字段判断"
    return status


def main():
    parser = argparse.ArgumentParser(description="探测小米账号验证码风控/配额状态")
    parser.add_argument("--send", action="store_true", help="尝试发送验证码 (会发短信)")
    parser.add_argument("--interval", type=int, default=0, help="循环探测间隔秒数 (0=单次)")
    parser.add_argument("--env", default=".env", help="环境文件路径 (默认 .env)")
    args = parser.parse_args()

    env = load_env(args.env)
    user = env.get("MIWIFI_XIAOMI_USER")
    password = env.get("MIWIFI_XIAOMI_PASS")
    if not user or not password:
        print("错误: .env 中缺少 MIWIFI_XIAOMI_USER / MIWIFI_XIAOMI_PASS")
        sys.exit(1)

    first = True
    while True:
        if not first:
            print(f"\n--- 等待 {args.interval}s 后再次探测 ---")
            time.sleep(args.interval)
        first = False
        result = probe(user, password, send=args.send)
        print(json.dumps(result, ensure_ascii=False, indent=1))
        if result.get("phase") == "ready" and not args.send:
            print("提示: 可发送验证码了。在 WebUI 点击\"更新 token\"完成自动续期测试。")
        if args.interval <= 0:
            break


if __name__ == "__main__":
    main()
