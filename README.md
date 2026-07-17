# crypto-msgflow

**Crypto message-flow research system** — 加密货币信息流研究系统。

## 设计理念

从信息流到判断流，再到回测循环。核心问题不是"爬什么"，而是"你当时怎么判断的，后来对了吗？"

### P2 原则：沉默比崩溃更危险

监控系统最致命的失败不是崩溃（你立刻知道），而是**静默返回空数据**——你以为"今天没新闻"，实际上是采集器 429 / 反爬 / schema drift，但它没报错。

所以 `collector_runs` 表区分 4 种状态：
- `ok` — 正常
- `empty` — 抓到了，但确实没数据
- `stale` — 抓到了，但最新数据太旧（上游可能在返回缓存）
- `error` — 崩了

**永远不要让 empty 和 error 看起来一样**。

### P0 数据：不可重取的时刻

| 数据 | 为什么是 P0 |
|---|---|
| OKX OI history | API 窗口有限，今晚不采=永久丢失 |
| X 推文 | 会删推、会封号 |
| HY OAS | 市场情绪指标，历史数据有回测价值 |
| **你的判断记录** | 永远不可能重来 |

## 数据模型

DuckDB 单文件 (`research.db`)。4 张核心表：

1. **`macro_series`** — 宏观时序（FRED / Treasury）
2. **`oi_funding`** — OKX 持仓量 + 资金费率（拥挤度）
3. **`events` / `sightings`** — 事件归并（content hash 去重）
4. **`verdicts`** — 判断记录 + realized return 回填

## 目录结构

```
crypto-msgflow/
├── collectors/          # 数据采集器
│   ├── base.py         # BaseCollector 基类
│   ├── fred.py         # 宏观数据（FRED）
│   ├── tga.py          # 财政部 TGA 日频
│   ├── okx_oi.py       # OKX OI + funding (P0)
│   ├── rss.py          # RSS 新闻
│   ├── x_kol.py        # X/Twitter KOL (P0)
│   ├── dedup.py        # content hash + coin 抽取
│   ├── events.py       # events/sightings 写入
│   └── alerts.py       # 告警通道
├── scripts/
│   ├── init_db.py      # 初始化 schema
│   ├── create_verdict.py
│   └── backfill_verdicts.py
├── tests/smoke/        # 每日 smoke tests
│   └── test_health.py
├── config/
│   ├── example.toml
│   └── secrets.toml    # (gitignored)
└── research.db         # (gitignored)
```

## 快速开始

### 1. 安装依赖

```bash
pip install duckdb httpx feedparser pydantic pydantic-settings
```

Python 3.11+ 推荐（内置 `tomllib`），否则需要 `pip install tomli`。

### 2. 初始化配置

```bash
cp config/example.toml config/secrets.toml
# 编辑 secrets.toml，填入 API keys
```

### 3. 初始化数据库

```bash
python3 scripts/init_db.py
```

### 4. 手动跑一次采集器

```bash
python3 collectors/fred.py
python3 collectors/okx_oi.py
```

### 5. 查看数据

```python
import duckdb
conn = duckdb.connect('research.db')

# 查看 FRED 数据
conn.execute("SELECT * FROM macro_series WHERE series_id='RRPONTSYD' ORDER BY obs_date DESC LIMIT 5").fetchdf()

# 查看 OI
conn.execute("SELECT * FROM oi_funding WHERE inst_id='BTC-USDT-SWAP' ORDER BY ts DESC LIMIT 5").fetchdf()

# 查看采集器健康度
conn.execute("SELECT * FROM collector_runs ORDER BY started_at DESC LIMIT 10").fetchdf()
```

## 生产部署

### Cron 定时任务

```cron
# FRED 宏观数据（UTC 13:00 = 美东上午）
0 13 * * * cd /path/to/crypto-msgflow && python3 collectors/fred.py

# OKX OI (P0: 每 5 分钟)
*/5 * * * * cd /path/to/crypto-msgflow && python3 collectors/okx_oi.py

# X KOL (P0: 每 10 分钟)
*/10 * * * * cd /path/to/crypto-msgflow && python3 collectors/x_kol.py

# RSS 新闻
*/10 * * * * cd /path/to/crypto-msgflow && python3 collectors/rss.py
```

### Smoke tests

每天跑一次，验证：
1. 数据存在
2. Schema 没漂移
3. **数据足够新鲜**（最关键）

```bash
pytest tests/smoke/test_health.py
```

## API 要求

| 服务 | 认证方式 | 备注 |
|---|---|---|
| FRED | API key（免费） | https://fred.stlouisfed.org/docs/api/api_key.html |
| Treasury Fiscal Data | 无需认证 | |
| OKX | 无需认证（公开 API） | 需要代理（见下） |
| twitterapi.io | API key（付费） | https://twitterapi.io |

### 代理配置

OKX 在某些数据中心 IP 段被墙。`BaseCollector` 实现了 per-source 代理策略：

- `use_env_proxy=True` — 信任环境变量 `HTTP_PROXY` / `HTTPS_PROXY`（OKX 需要）
- `use_env_proxy=False` — 绕过代理，直连（美国政府源 FRED/Treasury 需要）

项目使用 mihomo 作为全局代理，环境变量由 systemd drop-in 注入。

## 设计文档

完整设计思路见项目根目录的设计文档（Markdown），包含：
- §1-§2: 数据模型与时区约定
- §3: BaseCollector 契约
- §4: 各采集器规格
- §5: 去重机制（content hash）
- §6: 告警与 smoke tests

## License

MIT

## 作者

研究工具，仅供个人使用。不构成投资建议。
