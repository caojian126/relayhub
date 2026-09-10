# 🛰️ RelayHub

> 把多个 AI 中转站聚合成**一个 OpenAI 兼容接口**，免费额度不再散落在十几个后台里。

[![Deploy on Zeabur](https://img.shields.io/badge/Deploy-Zeabur-6E3AFF?style=for-the-badge)](https://zeabur.com/new)
![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-green)

---

## 它解决什么问题

你可能攒了十几个中转站，每个都有额度、都便宜、但都要单独配置。

RelayHub 把它们合成一个入口：**客户端只填一个 Base URL + 一个 Key**，后面自动在这些站之间挑最划算的那个，挂了就换下一个。

```
Cherry Studio / Rikkahub / NextChat / 你的脚本
                    │
                    │  https://你的域名/v1
                    ▼
        ┌──────── RelayHub ────────┐
        │  Key 鉴权                 │
        │  → 挑候选（站点 × 模型）    │
        │  → 排序（按额度/优先级）    │
        │  → 打第一个               │
        │  → 失败自动换下一个         │
        │  → 熔断 / 每日限额        │
        │  → 用量落库 + 日志         │
        └───────────┬──────────────┘
                    ▼
   站A(new-api) │ 站B │ 站C │ ...
```

## 功能

| 功能 | 说明 |
|---|---|
| **统一入口** | OpenAI 兼容 `/v1/chat/completions`（含流式）与 `/v1/models` |
| **跨站负载** | 同一模型配到多个站点，自动在这些站之间分配 |
| **三种策略** | `balanced` 按剩余额度加权 · `priority` 固定优先级 · `round_robin` 轮流 |
| **失败切换** | 4xx / 5xx / 超时 → 自动换下一个上游 |
| **熔断** | 连续失败 N 次自动冷却 M 分钟，到点自愈 |
| **每日限额** | 按「站点 × 模型」单独设，专治那些额度抠搜的站 |
| **额度查询** | 定时巡检查询各站剩余额度（new-api / one-api 系通用） |
| **用量统计** | 按天请求量 / token / 成功率图表 |
| **分站日志** | 每次请求（含失败与重试）都记录到具体哪个站 |
| **多 API Key** | 可以只给自己用，也可以发给别人，每个 Key 可单独限流 |
| **单文件管理面板** | 零构建、零 CDN 依赖，深色主题 |

## Zeabur 部署

### 1. 建服务

Zeabur → **New Project** → **Deploy from GitHub** → 选这个仓库（自动识别 `Dockerfile`）。

或者先 Fork 到你自己账号再部署。

### 2. ⚠️ 挂持久卷（必做）

服务 → **Storage / 存储** → 添加 Volume，**挂载路径填 `/data`**。

> 不挂的话，容器一重启你配的站点、Key、日志全没了。

### 3. 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `ADMIN_USERNAME` | 否 | 面板账号，默认 `admin` |
| `ADMIN_PASSWORD` | 否 | 面板密码。**留空会随机生成并打印在部署日志里** |
| `SECRET_KEY` | **建议** | 登录态签名密钥，随便一串长随机字符。不设的话每次重新部署都要重新登录 |
| `TZ` | 否 | 默认 `Asia/Shanghai`，决定「每日限额」几点重置、图表按哪天切分 |
| `DATA_DIR` | 否 | 默认 `/data` |
| `REQUEST_TIMEOUT` | 否 | 上游请求超时秒数，默认 600（中转站排队慢，别调太小） |

### 4. 打开面板

访问 `https://你的域名/admin`，用上面的账号登录。

## 使用流程

1. **加站点** —— 「站点」页填名称 + Base URL + API Key。
   Base URL 填 `https://xxx.com` 或 `https://xxx.com/v1` 都行。
2. **测连通性** —— 点「测试」，会去拉上游 `/v1/models`。成功的话可以一键把所有模型导入「路由」。
3. **设限额** —— 「路由」页给每个「站点 × 模型」组合设每日上限（0 = 不限）。
   同一个模型名配到多个站点 → 自动负载均衡 + 故障切换。
4. **建 Key** —— 「API 密钥」页生成 `sk-rh-...`。只给自己用就建一个。
5. **配置客户端** —— Base URL 填 `https://你的域名/v1`，Key 填上面生成的。

## 三种路由策略怎么选

| 策略 | 行为 | 适合 |
|---|---|---|
| `balanced` | 额度已知的站里，余额多的优先；额度未知的按优先级排后面 | 站点多、额度能查到（**推荐**） |
| `priority` | 严格按你排的优先级，第一个能打就一直用它 | 想让某个站当主力，其他当备份 |
| `round_robin` | 按今日已用次数轮流 | 站点额度都不大，想均匀消耗 |

> `balanced` 下，「额度未知」的站点会排在「额度已知」的后面。如果你所有站都查不到额度，它等价于按优先级。

## 行为细节

**失败切换**
单次请求最多尝试 `max_attempts` 个上游（默认 3），中间任何一个返回非 200 或超时都会自动换下一个。

**流式的边界**
一旦上游开始返回数据，就没法再切换了——客户端已经收到一半内容。所以：
- 切换只发生在「请求发出前」和「还没吐第一个字节」时
- 已经吐字后中断，会记为一次失败（并计入熔断）

**每日限额怎么算**
按「站点 × 模型」计数，**每次向上游发出请求都计一次，含失败的**。这样即使某个站一直挂，也不会反复消耗它的额度。次日 0 点（按 `TZ`）重置。

**熔断**
站点连续失败达到 `circuit_threshold` 次 → 冷却 `circuit_cooldown` 秒，期间不参与路由，到点自动恢复。面板上可以手动「解熔断」。

**额度查询**
定时调用各站的 `/v1/dashboard/billing/subscription` 与 `/v1/dashboard/billing/usage`（new-api / one-api 系通用）。
有些站把这俩接口关了，那就显示「未知」，**不影响路由**。

## 本地跑

```bash
pip install -r requirements.txt

ADMIN_PASSWORD=admin123 DATA_DIR=./data \
  uvicorn app.main:app --host 0.0.0.0 --port 8080

# 打开 http://localhost:8080/admin
```

## Docker

```bash
mkdir -p data
docker build -t relayhub .
docker run -d --name relayhub -p 8080:8080 \
  -v $(pwd)/data:/data \
  -e ADMIN_PASSWORD=admin123 \
  -e SECRET_KEY=$(openssl rand -hex 32) \
  relayhub
```

## 项目结构

```
app/
├── config.py       # 环境变量
├── db.py           # SQLite + 表结构（WAL 模式）
├── security.py     # 密码哈希 / 会话令牌 / Key 生成
├── engine.py       # 核心：候选筛选、排序、限额、熔断
├── quota.py        # 上游额度巡检
├── main.py         # FastAPI 路由：OpenAI 兼容层 + 管理 API
└── static/index.html   # 管理面板（单文件 SPA）
```

## 说明

- 数据默认存 SQLite（`/data/relayhub.db`），几十个站点完全够用。
- 不做任何自动签到 / 抓 Cookie。签到请自己在各站后台完成。
- 上游 Key 明文存在你自己的数据库里，请确保 `/data` 卷不对外暴露。

## Roadmap

- [ ] 模型名统一映射（各站写法不同 → 客户端只用逻辑名）
- [ ] `/v1/embeddings`、`/v1/images` 等端点转发
- [ ] 每个 Key 的速率限制（次/分钟）
- [ ] 按 token 而非次数计费的上限
- [ ] Anthropic 格式入口

## License

MIT
