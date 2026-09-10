# 部署指南

本文面向第一次部署 RelayHub 的人，从零到能跑通大约 10 分钟。

当前版本：**v0.4.0**

---

## 目录

1. [准备工作](#1-准备工作)
2. [Zeabur 部署（推荐）](#2-zeabur-部署推荐)
3. [首次登录与初始化](#3-首次登录与初始化)
4. [接入中转站](#4-接入中转站)
5. [同步模型](#5-同步模型)
6. [创建 API Key](#6-创建-api-key)
7. [客户端怎么填](#7-客户端怎么填)
8. [开启缓存](#8-开启缓存)
9. [日常运维](#9-日常运维)
10. [故障排查](#10-故障排查)
11. [本地 / 自建 Docker 部署](#11-本地--自建-docker-部署)

---

## 1. 准备工作

先确认三件事：

| 准备项 | 说明 |
|---|---|
| 一个 Zeabur 账号 | 已经能创建项目的就可以 |
| 若干中转站的 Base URL + API Key | 填进面板用，不会进环境变量 |
| 想好一个面板密码 | 这个会放环境变量，忘了也能找回来 |

---

## 2. Zeabur 部署（推荐）

### 2.1 把仓库 Fork 到自己账号

打开 https://github.com/caojian126/relayhub ，右上角 **Fork**。

> 不 Fork 也行，直接部署原仓库；但 Fork 一份方便你自己改。

### 2.2 新建项目

Zeabur 控制台 → **New Project** → 选一个区域 → **Deploy from GitHub** → 选中刚才的仓库。

Zeabur 会自动识别仓库根目录的 `Dockerfile`，不需要手动指定构建方式。

### 2.3 ⚠️ 挂载持久卷（最关键的一步）

服务页面 → **Storage / 存储** → **添加 Volume**：

| 项 | 填什么 |
|---|---|
| Mount Path（挂载路径） | **`/data`** |
| 容量 | 1 GB 就够 |

**只挂这一个路径，不要挂 `/app` 之类的地方。**

挂载点里会存两样东西：

- `relayhub.db` —— 站点、API Key、路由、日志、缓存
- `secret_key` —— 自动生成的登录态密钥（若未设 `SECRET_KEY`）

> 不挂卷的后果：每次重新部署，站点配置、Key、日志、缓存全部清空。

### 2.4 环境变量

服务页面 → **Environment Variables / 环境变量**：

| 变量 | 必填 | 填什么 |
|---|---|---|
| `ADMIN_PASSWORD` | **强烈建议** | 你的面板登录密码 |
| `ADMIN_USERNAME` | 否 | 面板账号，默认 `admin` |
| `SECRET_KEY` | 建议 | 随便一串长随机字符，如 64 位十六进制 |
| `TZ` | 建议 | `Asia/Shanghai` |
| `DATA_DIR` | 否 | 默认已经是 `/data` |
| `REQUEST_TIMEOUT` | 否 | 默认 `600` 秒，中转站排队慢的话可以调大 |

**设计原则：**

- **面板密码放环境变量** —— 忘记密码时在部署平台改一下、重启就能找回，不会被锁在面板外面
- **中转站 API Key 不放环境变量** —— 它们在面板里添加，存在持久卷的数据库里

`ADMIN_PASSWORD` 是**最高优先级**：只要它非空，每次启动都会把数据库里的密码校正成它。

### 2.5 开启公网访问

服务页面 → **Networking / 网络** → **Generate Domain**（生成域名）。

端口填 **8080**（容器内应用监听的端口）。Zeabur 会注入 `PORT` 环境变量，应用会自动适配，你不需要手动设 `PORT`。

部署完成后访问 `https://你生成的域名/healthz`，返回下面的内容就说明活了：

```json
{"ok": true, "version": "0.4.0", "time": 1757500000.0}
```

---

## 3. 首次登录与初始化

打开 `https://你的域名/admin`。

- 如果你设了 `ADMIN_PASSWORD`，用 `ADMIN_USERNAME`（默认 `admin`）+ 那个密码登录
- 如果没设，密码是随机生成的，去 **服务 → 日志** 里找：

```
================================================================
[RelayHub] 已创建面板账号: admin
[RelayHub] 随机初始密码: xxxxxxxxxxxxxx
[RelayHub] 建议设置环境变量 ADMIN_PASSWORD，以免忘记后进不去
================================================================
```

---

## 4. 接入中转站

左侧切到 **「站点」** 页，填：

| 字段 | 说明 |
|---|---|
| 名称 | 自己看得懂就行，如 `站A` |
| Base URL | `https://api.example.com` 或 `https://api.example.com/v1` **都行**，程序会自动补 `/v1` |
| API Key | 中转站给你的 `sk-...` |
| 优先级 | 数字越小越优先，默认 100 |
| 启用 | 是 |

填完点 **保存**，然后点这一行的 **「拉模型」**：

- 成功 → 会告诉你发现了几个模型，可以一键全部导入路由
- 失败 → 把提示信息（HTTP 状态码 + 返回内容）贴出来排查

站点列表里还能：

- **查额度** —— 立即拉一次剩余额度（支持 new-api / one-api 系）
- **解熔断** —— 站点被熔断后手动恢复
- **编辑** —— 改地址、换 Key（Key 留空表示不修改）

---

## 5. 同步模型

左侧切到 **「模型」** 页，点 **「扫描全部站点」**。

弹层里会按站点分组列出所有拉到的模型：

- 已导入的会置灰并标「已导入」，**不会覆盖你已经配置过的上游名和限额**
- 未导入的默认勾选
- 底部可以设 **默认每日上限**，导入时一次性套用

点「导入选中的 N 个」完成。

### 关于「站点 × 模型」的每日上限

路由列表里每条都能单独设：

- `0` = 不限制
- 大于 `0` = 该站点在**该模型**上每天最多被调用这么多次

**计数规则：每次向上游发出请求都计一次，包含失败的请求。** 这样即使某个站持续故障，也不会反复消耗它的额度。次日 0 点（按 `TZ`）重置。

### 关于负载均衡

同一个模型名导入到多个站点 → 请求会自动在这些站点之间分配。分配策略在「设置」页：

| 策略 | 行为 |
|---|---|
| `balanced` | 额度已知的站点中，余额多的优先（默认） |
| `priority` | 严格按优先级排序，第一个可用的就一直用 |
| `round_robin` | 按当日已用次数轮流 |

---

## 6. 创建 API Key

左侧切到 **「密钥」** 页。

> ⚠️ **如果你还没有创建任何 Key，接口处于「开放模式」——任何人都能调用你的接口。** 面板顶部会有红字提醒。

点「生成」创建一个（可以给每个设备/每个人单独建一个，各自统计）。

生成的 Key 形如 `sk-rh-xxxxxxxx`。

同一页上方有 **「接口地址」** 卡片，列出四个入口的完整 URL，可以直接对照填写。

---

## 7. 客户端怎么填

四个入口共享同一套中转站、路由、限额和缓存。按你的客户端类型选一个：

| 协议 | 方法 | 路径 | 客户端 Base URL 填 |
|---|---|---|---|
| OpenAI | POST | `/v1/chat/completions` | `https://你的域名/v1` |
| OpenAI Responses | POST | `/v1/responses` | `https://你的域名/v1` |
| Anthropic | POST | `/v1/messages` | `https://你的域名` |
| Gemini | POST | `/v1beta/models/{模型}:generateContent` | `https://你的域名` |

认证统一用：

```
Authorization: Bearer sk-rh-你的密钥
```

Gemini 客户端也支持把 Key 放在 URL 上：`?key=sk-rh-你的密钥`。

模型名填你在「模型」页导入的那个名字。

### 自测命令

**OpenAI 格式（流式）**

```bash
curl -N https://你的域名/v1/chat/completions \
  -H "Authorization: Bearer sk-rh-你的密钥" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-2.5-pro","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

**Anthropic 格式**

```bash
curl -N https://你的域名/v1/messages \
  -H "x-api-key: sk-rh-你的密钥" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-2.5-pro","max_tokens":256,"messages":[{"role":"user","content":"你好"}]}'
```

> Anthropic 客户端把它自己的 Key 放在 `x-api-key` 头里，RelayHub 会同时认 `Authorization: Bearer` 和 `x-api-key` 两种写法。若你的客户端把 Key 写死在 `x-api-key`，直接把 `sk-rh-...` 填进去即可。

**Gemini 格式**

```bash
curl -N "https://你的域名/v1beta/models/gemini-2.5-pro:generateContent?key=sk-rh-你的密钥" \
  -H "Content-Type: application/json" \
  -d '{"contents":[{"role":"user","parts":[{"text":"你好"}]}]}'
```

流式把 `:generateContent` 换成 `:streamGenerateContent`。

**Responses 格式**

```bash
curl -N https://你的域名/v1/responses \
  -H "Authorization: Bearer sk-rh-你的密钥" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-2.5-pro","input":"你好","stream":true}'
```

---

## 8. 开启缓存

**缓存默认是关的**，需要在「设置」页手动开。

### 原理

```
OpenAI / Responses / Anthropic / Gemini   四种客户端格式
        ↓ 翻译成统一的 OpenAI 请求体
     【 缓 存 】  key = 统一请求体的哈希
        ↓ 未命中才打上游
      中转站
        ↓ 翻译回客户端的格式
```

**缓存在「统一请求体」这一层，所以四种格式共享同一份缓存。** 用一个格式问过的问题，换另一种格式再问也能命中。

### 设置项

| 设置 | 说明 | 建议 |
|---|---|---|
| 缓存开关 | 总开关 | 开 |
| 仅缓存 `temperature=0` | 只缓存确定性请求，避免返回错误答案 | **开**（推荐） |
| 有效期 | 秒，`0` = 永不过期 | 3600 |
| 最大条目数 | 超出后按最近最少使用淘汰 | 1000 |
| 允许读写缓存的入口 | 不勾的入口完全绕过缓存 | 按需，至少勾 OpenAI |

### 注意

- 如果客户端不传 `temperature`，默认是 1（有随机性），此时**不会**被缓存。想让缓存生效，客户端要把温度设为 0
- 关了「仅缓存 temperature=0」后，只要请求体完全一致就命中——**上下文不同的对话可能拿到相同回复**，谨慎使用
- 命中缓存不消耗中转站额度，也不计入站点每日限额
- 概览页和日志里都能看到命中情况（日志的「来源」列会显示「缓存」）

---

## 9. 日常运维

### 概览页

今日请求数、成功率、缓存命中率、缓存条目数、Token 用量、近 7 天柱状图、站点状态一览。

### 日志页

每次请求（含失败和重试）都会记录，可以看到：入口格式、实际打到了哪个站点（或命中缓存）、用了哪个 Key、第几次尝试、耗时、Token、错误内容。支持按站点筛选和自动刷新。

### 自动同步模型

默认开启，24 小时一次，**只往路由里新增上游新出现的模型，绝不删你配过的东西**。可在设置页关掉或改间隔。

### 额度巡检

定时调用各站的 `/v1/dashboard/billing/*` 查询剩余额度。部分站点关闭了这两个接口，此时显示「未知」，**不影响路由**。

### 备份

设置页 → **导出配置**，会下载包含站点、Key、路由、设置的 JSON。换服务器时用 **导入配置** 恢复。

> 导出的 JSON 包含明文密钥，请妥善保管。

---

## 10. 故障排查

### 面板打不开 / 502

1. 先访问 `/healthz`，能返回 JSON 说明应用是活的，问题在网络或域名
2. 检查服务的公网端口是否设成了 `8080`
3. 看服务日志有没有启动报错

### 登录不上

- 设了 `ADMIN_PASSWORD` 却登不上 → 环境变量改完要**重启服务**才生效
- 没设 → 去服务日志里找随机密码
- 实在不行 → 设一个新的 `ADMIN_PASSWORD` 并重启，密码会被强制校正

### 调用返回 401

```
{"detail":"缺少 API Key"}  → 没带 Authorization 头
{"detail":"API Key 无效或已禁用"} → Key 填错了，或者被你在面板里停用了
```

### 调用返回 503

```
没有可用上游支持模型 xxx
```

模型名没配到路由里，或者配了但站点全被停用/熔断/达到每日上限。去「模型」页确认这个名字存在。

### 调用返回 502

```
所有上游均失败。最后错误：...
```

后面会带上每个站的具体错误。常见原因：

- 中转站的 Key 过期或余额耗尽（返回 401 / 402 / 403）
- 该站不支持你请求的这个模型（返回 404 / 400）
- 该站被限流（返回 429）

去「日志」页能看到每一次尝试的完整错误。

### 某个站点连续失败后进入冷却

那是熔断：连续失败达到阈值（默认 3 次）后会冷却一段时间（默认 600 秒），期间不参与路由，到点自动恢复。可以在「站点」页点 **解熔断** 立即恢复，或者在设置页调整阈值和冷却时间。

### 缓存不生效

按顺序检查：

1. 设置页的「缓存开关」开了吗
2. 当前入口在这个「允许读写缓存的入口」名单里吗
3. 请求里的 `temperature` 是 `0` 吗（开了「仅缓存 temperature=0」时）
4. 请求体是否完全一致（系统提示词、历史消息、参数任何一个不同都算不同请求）
5. 看日志的「来源」列，命中时会显示「缓存」标签

### 流式请求中途断了

一旦上游开始返回内容，就不能再切换到其他站点（客户端已经收到一半内容了）。切换只发生在「请求前」和「还没返回第一个字节」时。已经吐字后中断会记一次失败并计入熔断。

---

## 11. 本地 / 自建 Docker 部署

### Docker Compose

```yaml
services:
  relayhub:
    build: https://github.com/caojian126/relayhub.git
    container_name: relayhub
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      - ADMIN_USERNAME=admin
      - ADMIN_PASSWORD=换成一个强密码
      - SECRET_KEY=换成一串长随机字符
      - TZ=Asia/Shanghai
      - DATA_DIR=/data
    volumes:
      - ./data:/data
```

```bash
docker compose up -d
```

访问 http://localhost:8080/admin

### 直接跑 Python

```bash
pip install -r requirements.txt

DATA_DIR=./data \
ADMIN_PASSWORD=admin123 \
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

### 反向代理注意事项

如果用 Nginx / Caddy 在前面，务必**关闭对 SSE 的缓冲**，否则流式会变成一次性输出：

Nginx：

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 600s;
    chunked_transfer_encoding on;
}
```

Caddy 默认不缓冲，一般无需额外配置。

---

## 附：环境变量总表

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `ADMIN_PASSWORD` | 强烈建议 | 随机生成 | 面板密码，最高优先级，忘了就改它 |
| `ADMIN_USERNAME` | 否 | `admin` | 面板账号，不设则不干预面板里改过的账号 |
| `SECRET_KEY` | 建议 | 自动生成 | 登录态签名密钥，不设则随机生成并存在卷里 |
| `TZ` | 建议 | `Asia/Shanghai` | 影响每日限额重置时间与图表按天切分 |
| `DATA_DIR` | 否 | `/data` | 数据目录，Dockerfile 里已经写死 |
| `DB_PATH` | 否 | `$DATA_DIR/relayhub.db` | 数据库路径 |
| `CONNECT_TIMEOUT` | 否 | `15` | 连接上游超时（秒） |
| `REQUEST_TIMEOUT` | 否 | `600` | 上游整体请求超时（秒） |
| `PORT` | 否 | `8080` | 监听端口，Zeabur 会自动注入 |
