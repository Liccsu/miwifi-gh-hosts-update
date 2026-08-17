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

### 方式一（推荐）：WebUI 交互授权

token 缺失或失效时，程序输出授权链接并进入等待状态，WebUI（默认
`http://<服务器>:8080`）页面会显示"需要授权"面板：

1. 点击页面上的授权链接，浏览器打开小米账号登录页
2. 登录小米账号并授权（新设备首次需短信验证码确认，属安全流程）
3. 授权后浏览器跳转到 s.miwifi.com，复制地址栏完整 URL
4. 粘贴到 WebUI 输入框提交

程序自动换取并缓存新 token（`TOKEN_CACHE_FILE`，已挂载卷）。token 有效期
约 90 天，到期后重复上述操作，全程无需 SSH 进服务器改文件。

> 若不想用 WebUI：授权后把回跳 URL 写入宿主机挂载目录
> `./data/authorize.url` 同样有效。

### 方式二：账号自动刷新（仅限已信任设备）

在 `.env` 中配置 `MIWIFI_XIAOMI_USER` 与 `MIWIFI_XIAOMI_PASS`，程序启动时
自动执行 passport 登录 -> OAuth 授权 -> 换取 `access_token` 并缓存，
60 天（`TOKEN_REFRESH_INTERVAL`）主动刷新、失效即时刷新。

> 限制：小米对新设备/异地环境登录强制安全验证（安全手机短信验证码），
> 纯 API 无法完成，此时自动刷新会失败并自动转入方式一的 WebUI 授权。
> 在已信任设备/网络（如家庭 NAS）上运行可全自动。

### 方式三：手动 token

浏览器打开自定义 Hosts 页面（`http://s.miwifi.com/dist/userhosts/index.html`，
`gatewayIp` 改为路由器网关 IP），登录授权后，地址栏 `#access_token=...`
即 `MIWIFI_TOKEN`，`deviceID=...` 即 `MIWIFI_DEVICE_ID`。token 约 90 天
过期，程序会输出失效日志并转入 WebUI 授权流程。

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

### 3. 访问 WebUI

浏览器打开 `http://<服务器IP>:8080`，可查看同步状态与 token 有效期；
token 失效需要授权时页面会显示授权入口（见下文"获取与更新 token"）。


### 环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `MIWIFI_XIAOMI_USER` | 二选一 | - | 小米账号，账号自动刷新模式 |
| `MIWIFI_XIAOMI_PASS` | 二选一 | - | 小米账号密码，账号自动刷新模式 |
| `MIWIFI_TOKEN` | 二选一 | - | 手动 access_token，来自页面 URL |
| `MIWIFI_DEVICE_ID` | 是 | - | 路由器设备 ID，来自页面 URL 的 `deviceID` |
| `MIWIFI_APP_ID` | 否 | `2882303761517675329` | 应用 ID（页面固定值） |
| `MIWIFI_SCOPE` | 否 | `1+1000+3` | 授权 scope（页面固定值） |
| `TOKEN_REFRESH_INTERVAL` | 否 | `5184000` | token 主动刷新周期（秒），仅账号模式 |
| `TOKEN_CACHE_FILE` | 否 | `/data/token.json` | token 缓存路径（已挂载卷） |
| `WEBUI_PORT` | 否 | `8080` | WebUI 端口（compose 已映射） |
| `WEBUI_TOKEN` | 否 | 空 | WebUI 访问 token，设置后需 `?token=` 或 `X-Token` 头 |
| `WEBUI_DISABLE` | 否 | 空 | 设为 `1` 禁用 WebUI |
| `AUTHORIZE_FILE` | 否 | `/data/authorize.url` | 授权回跳 URL 文件通道 |
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
