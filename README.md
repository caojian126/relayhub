# RelayHub

把多个 AI 中转站聚合成一个 OpenAI 兼容接口。

客户端只需要配置一个 Base URL 和一个 Key，RelayHub 负责在多个上游之间做负载均衡、故障切换和用量限额。

作者：**草翦**

[![Deploy on Zeabur](https://img.shields.io/badge/Deploy-Zeabur-6E3AFF?style=flat-square)](https://zeabur.com/new)
![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?style=flat-square&logo=fastapi&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green?style=flat-square)

---

## 背景

中转站通常比官方便宜，但额度分散在多个后台。当站点数量变多之后，会面临几个问题：

- 每个站都要在客户端单独配置一遍
- 不知道哪个站还剩多少额度
- 某个站挂了，得手动切换
- 额度小的站容易被一次性用完

RelayHub 把这些统一到一个入口后面。

```
Cherry Studio / Rikkahub / NextChat / 自定义脚本
                    |
                    |  https://your-domain/v1
                    v
        +--------- RelayHub ---------+
        |  API Key 鉴权              |
        |  -> 筛选候选（站点 x 模型）  |
        |  -> 排序（额度 / 优先级）    |
        |  -> 请求第一个上游           |
        |  -> 失败自动切换下一个       |
        |  -> 熔断 / 每日限额         |
        |  -> 用量落库 + 分站日志      |
        +-------------+--------------+
                      v
       站点 A (new-api) | 站点 B | 站点 C | ...
```

## 功能

| 功能 | 说明 |
|---|---|
| 统一入口 | OpenAI 兼容 `/v1/chat/completions`（含流式）与 `/v1/models` |
| 跨站负载 | 同一个模型配置到多个站点后，自动在这些站点之间分配 |
| 三种路由策略 | `balanced` 按剩余额度加权 · `priority` 固定优先级 · `round_robin` 轮流 |
| 失败自动切换 | 上游返回 4xx / 5xx / 超时，自动换下一个 |
| 熔断保护 | 连续失败 N 次后自动冷却 M 分钟，到点自愈 |
| 每日限额 | 按「站点 x 模型」单独设置，用于限制额度较小的站点 |
| 模型自动同步 | 直接从各上游拉取 `/v1/models`，勾选即可导入，支持定时自动同步 |
| 额度巡检 | 定时查询各站剩余额度（new-api / one-api 系通用） |
| 用量统计 | 按天的请求量、Token、成功率图表 |
| 分站日志 | 每次请求（含失败与重试）都记录到具体站点 |
| 多 API Key | 可自用，也可分发，每个 Key 可单独限流 |
| 管理面板 | 单文件 SPA，零构建、无外部依赖 |
| 配置持久化 | 数据与配置全部落在持久卷，调整配置无需重新部署 |

## 部署

### 1. 创建服务

Zeabur -> New Project -> Deploy from GitHub -> 选择本仓库（自动识别 `Dockerfile`）。

也可以先 Fork 到自己的账号再部署。

### 2. 挂载持久卷（必须）

服务 -> Storage -> 添加 Volume，**挂载路径填写 `/data`**。

> 不挂载持久卷的话，容器每次重启都会丢失站点配置、API Key 和日志。

### 3. 环境变量

全部为可选项，仅用于首次初始化。

| 变量 | 说明 |
|---|---|
| `DATA_DIR` | 数据目录，默认 `/data` |
| `TZ` | 时区，默认 `Asia/Shanghai`。决定每日限额的重置时间与图表按天切分 |
| `ADMIN_USERNAME` | 首次初始化时的面板账号，默认 `admin` |
| `ADMIN_PASSWORD` | 首次初始化时的面板密码。留空则随机生成 `config.json`，内容可自行修改 |
| `SECRET_KEY` | 登录态签名密钥，可选 |
| `REQUEST_TIMEOUT` | 上游请求超时秒数，默认 `600` |

### 4. 打开面板

访问 `https://your-domain/admin`。

首次启动时，`/data/config.json` 会自动生成：

```json
{
  "admin": {
    "username": "admin",
    "password": "自动生成的随机密码"
  },
  "server": {
    "secret_key": "自动生成的密钥"
  }
}
```

直接编辑这个文件并重启服务即可生效，无需重新部署。密码也可以在面板的「设置」页修改，修改后会同步写回该文件。

## 使用流程

1. **添加站点**
   在「站点」页填写名称、Base URL（`https://xxx.com` 或 `https://xxx.com/v1` 均可）和 API Key。

2. **同步模型**
   在「模型」页点击「扫描全部站点」，RelayHub 会去拉取每个上游的 `/v1/models`。勾选需要的模型即可一键导入路由，无需手动填写模型名。

3. **设置限额**
   在路由列表中，可以针对每个「站点 x 模型」组合设置每日上限。设为 `0` 表示不限制。
   同一个模型名配置到多个站点后，请求会自动在这些站点之间负载与故障切换。

4. **创建 API Key**
   在「密钥」页生成 `sk-rh-...`。仅自己使用的话创建一个即可。

5. **配置客户端**
   Base URL 填 `https://your-domain/v1`，Key 填上一步生成的。

## 路由策略

| 策略 | 行为 | 适用场景 |
|---|---|---|
| `balanced` | 在额度已知的站点中，余额多的优先；额度未知的按优先级排在后面 | 站点较多且额度可查询（推荐） |
| `priority` | 严格按设定优先级，第一个可用的站点持续使用 | 指定某个站点为主力，其余作为备份 |
| `round_robin` | 按当日已用次数轮流 | 各站额度都不大，希望均匀消耗 |

> 在 `balanced` 策略下，额度未知的站点会排在额度已知的站点之后。若所有站点都查不到额度，其效果等同于按优先级排序。

## 行为说明

### 失败切换

单次请求最多尝试 `max_attempts` 个上游（默认 3）。中间任何一个返回非 200 或超时，都会自动切换到下一个。

### 流式请求的边界

一旦上游开始返回数据，就无法再切换站点——客户端已经收到部分内容。因此：

- 切换只发生在「请求发出前」和「尚未返回第一个字节」时
- 已经产生输出后中断，会记为一次失败并计入熔断

### 每日限额的计算方式

按「站点 x 模型」计数，**每次向上游发出请求都计一次，包含失败的请求**。这样即使某个站点持续故障，也不会反复消耗它的额度。次日 0 点（按 `TZ` 时区）重置。

### 熔断

站点连续失败达到 `circuit_threshold` 次后，进入 `circuit_cooldown` 秒的冷却，期间不参与路由，到点自动恢复。面板上可手动解除。

### 额度查询

定时调用各站的 `/v1/dashboard/billing/subscription` 与 `/v1/dashboard/billing/usage`（new-api / one-api 系通用）。
部分站点关闭了这两个接口，此时会显示「未知」，不影响路由。

## 本地运行

```bash
pip install -r requirements.txt

DATA_DIR=./data ADMIN_PASSWORD=admin123 \
  uvicorn app.main:app --host 0.0.0.0 --port 8080
```

打开 http://localhost:8080/admin

## Docker

```bash
mkdir -p data
docker build -t relayhub .
docker run -d --name relayhub -p 8080:8080 \
  -v $(pwd)/data:/data \
  -e SECRET_KEY=$(openssl rand -hex 32) \
  relayhub
```

## 项目结构

```
app/
├── config.py       环境变量
├── fileconfig.py   /data/config.json 读写
├── db.py           SQLite 表结构与访问层（WAL 模式）
├── security.py     密码哈希 / 会话令牌 / Key 生成
├── engine.py       核心：候选筛选、排序、限额、熔断
├── models_sync.py  模型发现与同步
├── quota.py        上游额度巡检
├── main.py         FastAPI 路由：OpenAI 兼容层 + 管理 API
└── static/index.html   管理面板（单文件 SPA）
```

## 安全说明

- 上游 API Key 以明文保存在 `/data/relayhub.db`，请确保持久卷不对外暴露
- 面板密码使用 PBKDF2-SHA256（120,000 轮）哈希存储
- 未创建任何 API Key 时，接口处于开放模式，任何人都可调用。建议部署完成后立即创建一个

## Roadmap

- [ ] 模型名统一映射（各站写法不同，客户端只使用逻辑名）
- [ ] `/v1/embeddings`、`/v1/images` 等端点转发
- [ ] 每个 Key 的速率限制（次/分钟）
- [ ] 基于 Token 用量的限额
- [ ] Anthropic 格式入口

## License

MIT
