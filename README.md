# crypto-msgflow

加密货币消息面采集系统。cron 驱动的哑进程把宏观、拥挤度、新闻、社交、
财报日历采进 DuckDB，供分析层只读消费。

## 功能

- **宏观时序**：美联储资产负债表、TGA、逆回购、利率、通胀、HY 信用利差、CPI、PCE、失业率、非农
- **拥挤度**：OKX 永续持仓量 + 资金费率 + 标记价
- **事件去重**：新闻/社交按标题 hash 归并，记录首见时间与传播广度
- **前瞻日历**：加密相关股财报日程、FOMC 议息日程
- **地缘信号**：Polymarket 预测市场隐含概率、OFAC 制裁地址黑名单、GDELT 宏观/地缘报道量+情绪
- **判断记录**：verdicts 表记录可证伪预测 + 回填实际收益
- **健康监控**：区分「真没数据」与「拿不到数据」，防静默失败

## 当前调通的渠道

| 渠道 | 采集器 | 数据 | 认证 |
|---|---|---|---|
| FRED | `fred` | 7 宏观 series（含 HY OAS） | API key（免费） |
| Treasury | `tga` | 日频 TGA 余额 | 无 |
| BLS | `bls` | CPI / 核心CPI / 失业率 / 非农 / 时薪 | API key（可选，升配额） |
| BEA | `bea` | PCE / 核心PCE | API key（免费，需激活） |
| OKX | `okx_oi` / `okx_oi_1h` / `okx_price` | BTC/ETH 永续 OI+funding（15m 实时 + 1h 骨架）、价格历史 | 无（公开行情） |
| RSS | `rss` | coindesk / cointelegraph / theblock / FOMC | 无 |
| X/Twitter | `x_kol` | KOL 推文 | twitterapi.io key |
| BigData | `bigdata` | COIN/MSTR/MARA/RIOT/HOOD/CLSK 财报 | API key |
| FOMC | `fomc` | 议息前瞻日程 | 无 |
| Polymarket | `polymarket` | Fed/CPI/BTC 预测市场隐含概率 | 无（公开 API） |
| OFAC | `ofac_addresses` | SDN 制裁加密地址黑名单 | 无（0xB10C GitHub） |
| GDELT | `gdelt` | 宏观/地缘报道量+情绪（BigQuery） | GCP 项目 + service account |
| 上币公告 | `listing_alert`(watcher) | OKX/Binance 新上币 → Lark | 无 |

**告警**：采集器故障/stale 与上币公告经 lark-cli（bot 身份）推送到专用飞书群，
与指挥通道隔离。cron `MAILTO` 邮件兜底。

**未接入**：CME FedWatch（数据中心 IP 被封）、FMP 经济日历（付费专属，已废弃）。

## 快速开始

```bash
# 依赖
pip install duckdb httpx curl-cffi feedparser pydantic pydantic-settings
# Python 3.11+ 自带 tomllib，否则 pip install tomli
# GDELT collector (optional): pip install google-cloud-bigquery

# 配置
cp config/example.toml config/secrets.toml
# 编辑 secrets.toml 填 API keys

# 建库
python3 scripts/init_db.py

# 跑单个采集器
python3 -m collectors.fred
python3 -m collectors.okx_oi
```

## 用法

### 跑采集器
每个采集器都是独立模块，`python3 -m collectors.<name>`：
```bash
python3 -m collectors.fred      # 宏观
python3 -m collectors.bls       # 劳工统计
python3 -m collectors.bea       # PCE
python3 -m collectors.okx_oi    # 拥挤度实时纹理（rt15，15 分钟一次）
python3 -m collectors.okx_oi_1h # OI 1h 骨架（一天两次，可回补 30 天窗）
python3 -m collectors.okx_price # 价格历史 1h OHLCV（verdict 回填用，可回溯）
python3 -m collectors.rss       # 新闻 + FOMC 声明
python3 -m collectors.x_kol     # X KOL
python3 -m collectors.bigdata   # 加密股财报日历
python3 -m collectors.fomc      # FOMC 议息日程
python3 -m collectors.polymarket      # Polymarket 预测市场隐含概率
python3 -m collectors.ofac_addresses  # OFAC 制裁地址黑名单
python3 -m collectors.gdelt           # GDELT 地缘/宏观报道量+情绪
```

### 新闻全文（按需 fetch，7 天 LRU）
```python
from collectors.fulltext import get_fulltext
body = get_fulltext(event_id, url)   # 缓存未命中才抓，curl_cffi TLS 伪装
```

### 上币告警（常驻进程，非 cron）
```bash
python3 -m watchers.listing_alert   # 5s 轮询，新上币推 Lark
# 生产用 systemd user service（见 deploy/msgflow-listing.service）
```

### 记录判断
```bash
python3 scripts/create_verdict.py "净流动性下降，BTC 4h 内回调" \
    --label INFERRED --confidence MED --predict-dir down --predict-window 4h
python3 scripts/backfill_verdicts.py   # 回填实际收益
```

### 查数据
```python
import duckdb
c = duckdb.connect('research.db')

# 净流动性输入（注意单位：WALCL/WTREGEN 百万美元，RRP 十亿美元）
c.execute("SELECT series_id, obs_date, value FROM macro_series "
          "WHERE series_id IN ('WALCL','WTREGEN','RRPONTSYD') "
          "AND source='fred' ORDER BY obs_date DESC LIMIT 9").fetchdf()

# 事件传播广度
c.execute("SELECT e.canonical_title, COUNT(DISTINCT s.source) breadth "
          "FROM events e JOIN sightings s USING(event_id) "
          "GROUP BY 1 ORDER BY breadth DESC LIMIT 10").fetchdf()

# 未来 FOMC 会议
c.execute("SELECT event_date, detail, has_press_conf FROM macro_calendar "
          "WHERE event_type='FOMC' AND event_date >= CURRENT_DATE "
          "ORDER BY event_date").fetchdf()

# 加密股财报日程
c.execute("SELECT ticker, event_datetime, fiscal_period FROM corp_events "
          "ORDER BY event_datetime").fetchdf()

# Polymarket Fed 决议市场隐含概率
c.execute("SELECT ts, outcome, implied_prob FROM polymarket_snapshots "
          "WHERE market_slug LIKE '%fed%interest%' "
          "ORDER BY ts DESC LIMIT 10").fetchdf()

# OFAC 最近新增制裁地址
c.execute("SELECT symbol, COUNT(*) AS new_count FROM ofac_crypto_addresses "
          "WHERE first_seen > CURRENT_TIMESTAMP - INTERVAL 7 DAY "
          "GROUP BY symbol").fetchdf()

# 采集器健康
c.execute("SELECT collector, status, rows_written, started_at "
          "FROM collector_runs ORDER BY started_at DESC LIMIT 12").fetchdf()
```

### 部署
```bash
# 编辑 scripts/crontab.example 后
crontab scripts/crontab.example
```

### 健康检查
```bash
python3 -m pytest tests/smoke/     # 每采集器三断言：存在性/契约/时效性
```

## 文档

- `spec.md` — 数据模型、采集器规格、健康监控
- `design.md` — 代码架构、控制流、扩展点
- `GEO_COLLECTORS.md` — 地缘三源实现摘要（Polymarket/OFAC/GDELT）

## 代理说明

数据中心 IP 部分源需 mihomo 代理，采集器按源分别配置（`use_env_proxy`）：
- **直连**：FRED / TGA / BEA（美国政府）
- **走代理**：OKX / BLS / RSS / BigData / FOMC / Polymarket / OFAC / GDELT

详见 `design.md` §3。Polymarket/OFAC/GDELT 在当前环境下 direct 全挂，必须走代理（mihomo 出口节点偶尔抖动，已加重试）。
