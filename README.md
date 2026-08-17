# miwifi-gh-hosts-update

云端自动同步 [GitHub-IP-hosts](https://github.com/ittuann/GitHub-IP-hosts) 数据到小米路由器自定义 Hosts。

容器化运行，GitHub Actions 自动构建并发布镜像，`docker compose` 一键部署。

## 工作原理

小米路由器新版固件（如 `xiaomi.router.rd15`）的"自定义 Hosts"功能没有本地接口，
页面托管在 `s.miwifi.com`，数据通过小米云服务 `gorouter.info` 转发写入路由器。
本程序逆向自该前端代码（`router_request_3.js` / `userhosts/index.js`），直接调用云接口：

- 读取：`GET https://www.gorouter.info/api-third-party/service/internal/custom_host_get`
- 写入：`POST https://www.gorouter.info/api-third-party/service/internal/custom_host_set`

鉴权方式：小米账号授权得到的 `access_token` 作为普通请求参数传递，无需 MAC 签名。

因此程序可在任意云主机/容器中运行，无需访问路由器所在局域网，只要路由器在线即可。

### 同步策略

- 从数据源（默认 GitHub-IP-hosts 的 `hosts` 文件，含 jsDelivr CDN 备用源）拉取内容
- 解析为 `IP 域名` 条目（去掉注释与空行，与页面逻辑一致）
- 读取路由器当前 hosts，**保留其中非托管域名的手动条目**，托管域名整体替换为最新数据
- 内容有变化才写入；写入前校验总长度（页面上限 35840 字符）
- 周期执行（默认 6 小时，可配置），无变化时跳过

## 获取与更新 token

1. 浏览器打开自定义 Hosts 页面（`http://s.miwifi.com/dist/userhosts/index.html`，`gatewayIp` 改为路由器网关 IP），登录小米账号授权
2. 地址栏 URL 中 `#access_token=...` 后面的值即 `MIWIFI_TOKEN`，`deviceID=...` 即 `MIWIFI_DEVICE_ID`
3. token 有效期约 90 天（URL 中 `expires_in=7776000` 秒）。过期后程序会持续输出
   `access_token 已失效` 错误日志，此时重复步骤 1 获取新 token，更新 `.env` 后重启容器即可，无需其他操作

## 部署

### 1. 准备配置

```bash
cp .env.example .env
# 编辑 .env, 填入 MIWIFI_TOKEN 与 MIWIFI_DEVICE_ID
```

### 2. 启动

```bash
docker compose up -d
```

查看日志：

```bash
docker compose logs -f
```

### 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `MIWIFI_TOKEN` | 是 | - | 小米账号授权 access_token，来自页面 URL |
| `MIWIFI_DEVICE_ID` | 是 | - | 路由器设备 ID，来自页面 URL 的 `deviceID` |
| `MIWIFI_APP_ID` | 否 | `2882303761517675329` | 应用 ID（页面固定值） |
| `MIWIFI_SCOPE` | 否 | `1+1000+3` | 授权 scope（页面固定值） |
| `GOROUTER_BASE_URL` | 否 | `https://www.gorouter.info` | 云服务地址 |
| `HOSTS_URLS` | 否 | raw.githubusercontent + jsDelivr 备用 | hosts 数据源，逗号分隔，按序尝试 |
| `SYNC_INTERVAL_SECONDS` | 否 | `21600` | 同步间隔（秒） |
| `HTTP_TIMEOUT` | 否 | `30` | 单次请求超时（秒） |
| `LOG_LEVEL` | 否 | `INFO` | 日志级别 |

## 镜像

GitHub Actions 在推送 `main` 分支或打 `v*` 标签时自动构建并发布多架构镜像
（linux/amd64、linux/arm64）到 GHCR：

- `ghcr.io/liccsu/miwifi-gh-hosts-update:latest`
- `ghcr.io/liccsu/miwifi-gh-hosts-update:v1.2.3`（标签）
- `ghcr.io/liccsu/miwifi-gh-hosts-update:sha-<commit>`（每次提交）

## 本地开发

```bash
# 单次同步（调试用）
MIWIFI_TOKEN=xxx MIWIFI_DEVICE_ID=yyy python -m app --once

# 运行单元测试
python -m unittest discover -s tests -v
```

无第三方依赖，仅用 Python 标准库。
