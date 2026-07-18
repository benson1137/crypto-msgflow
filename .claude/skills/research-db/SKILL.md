---
name: research-db
description: research.db 的地图——表结构、字段口径、常用只读查询。所有其他分析 skill 的地基。只讲"怎么取数据"，不讲"怎么判断"。
---

# research-db — 库的地图

crypto-msgflow 的 DuckDB 单文件在 `research.db`。所有采集器 append-only 写入，分析层**只读**。
本 skill 是取数地基：给出每张表的字段口径和可直接跑的查询。**它不下任何判断**——判断是 news-triage / priced-in-check / verdict-write 的事。

## 连接

```python
import duckdb
conn = duckdb.connect("research.db", read_only=True)   # 分析层永远 read_only
df = conn.execute("SELECT ...").fetchdf()
```

只读连接是硬约束：分析层写库会污染 append-only 语义，也会和 cron 采集器抢锁。

## 表清单（9 张）

| 表 | 内容 | 主键 | 粒度 |
|---|---|---|---|
| `macro_series` | 宏观时序（FRED/BLS/BEA/Treasury） | (series_id, obs_date, source) | 日/周/月 |
| `oi_funding` | OKX 持仓量 + 资金费率 | (inst_id, ts, granularity) | 1m / 1h |
| `events` | 归并后的事件（去重后唯一） | event_id (=content_hash) | — |
| `sightings` | 每个源看到某事件的记录 | (event_id, source, seen_ts) | — |
| `corp_events` | 加密股前瞻财报日历（BigData） | (entity_id, category, event_datetime) | 前瞻 |
| `macro_calendar` | FOMC 前瞻会议日历 | (event_type, event_date, source) | 前瞻 |
| `verdicts` | 判断记录（系统产出） | verdict_id | — |
| `news_fulltext` | 全文 7 天 LRU 缓存 | (event_id, url) | — |
| `collector_runs` | 采集健康日志 | (collector, started_at) | 每次运行 |

## 字段口径（易错点）

### macro_series
- `obs_date` 是**观测日**，不是抓取日。宏观数据 obs_date 天然滞后发布（月度数据可滞后 1–2 个月）。
- `value` 可为 NULL（FRED 用 `"."` 表示缺失，已转 NULL，**不是 0**）。
- `source` 进主键：同一指标可有多源。TGA 同时来自 `fred`（WTREGEN 周均）和 `treasury_fiscal`（日频），并存，下游自己选。
- 当前 series（按 source）：
  - `fred`: WALCL, WTREGEN, RRPONTSYD, DGS10, T10YIE, DFII10, BAMLH0A0HYM2(HY OAS)
  - `bls`: CUUR0000SA0(CPI), CUUR0000SA0L1E(核心CPI), LNS14000000(失业率), CES0000000001(非农), CES0500000003(时薪)
  - `bea`: PCEPI(PCE价格指数), PCEPILFE(核心PCE)
  - `treasury_fiscal`: TGA(日频)

> **单位陷阱（务必知情）**：净流动性 = WALCL − WTREGEN − RRPONTSYD，但
> **WALCL/WTREGEN 单位是百万美元，RRPONTSYD 是十亿美元**。直接相减差 1000 倍。
> 算之前先统一单位：`RRPONTSYD * 1000` 转成百万。

### oi_funding
- `ts` 是 **UTC**（全系统统一 UTC-naive）。
- `oi_usd` 是美元计持仓量，`oi_ccy` 是币本位。做分位/比较用 `oi_usd`。
- `funding_rate` 是当期费率（小数，非百分比）。
- 1m 数据保留 30 天，之后聚合成 1h（见 spec §7）。

### events / sightings
- `events.first_seen_ts` 是 priced-in 判据之一，**永不更新**。
- `events.coins` 是 `VARCHAR[]`，用 `list_contains(coins, 'BTC')` 过滤。
- **breadth（多少家在说）= sightings 里某 event_id 的 DISTINCT source 数**，事后重建不出来。
- `sightings.source` 形如 `rss:coindesk`、`x:@ki_young_ju`、`rss:fomc`。

### news_fulltext（全文按需取，别直接查表）
采集层**不存全文**，`sightings` 只有 url+title。要读正文走入口函数，不要直接
查 `news_fulltext` 表（它是缓存，未必有你要的那条）：
```python
from collectors.fulltext import get_fulltext
body = get_fulltext(event_id, url)   # 命中返回；未命中现 fetch 并缓存 7 天
```
返回 None = fetch 失败（付费墙/反爬/404），调用方自行处理。

### verdicts
- 唯一允许 UPDATE 的表（只 `realized_ret` / `realized_at` 两字段回填）。
- `label`: KNOWN|COMPUTED|INFERRED|COMMON|FRAME|GUESS
- `confidence`: HIGH|MED|LOW|VERY_LOW|UNKNOWN
- priced-in 快照字段（oi_pctile/funding_pctile/breadth）是判断当时状态，不可事后重算。

## 常用查询

### 净流动性（注意单位换算）
```sql
WITH latest AS (
  SELECT series_id, value,
         ROW_NUMBER() OVER (PARTITION BY series_id ORDER BY obs_date DESC) rn
  FROM macro_series
  WHERE series_id IN ('WALCL','WTREGEN','RRPONTSYD') AND source='fred' AND value IS NOT NULL
)
SELECT
  MAX(value) FILTER (WHERE series_id='WALCL')
  - MAX(value) FILTER (WHERE series_id='WTREGEN')
  - MAX(value) FILTER (WHERE series_id='RRPONTSYD') * 1000   -- 十亿→百万
  AS net_liquidity_musd
FROM latest WHERE rn=1;
```

### OI 90 日分位（priced-in 核心输入）
```sql
WITH hist AS (
  SELECT inst_id, oi_usd,
         PERCENT_RANK() OVER (PARTITION BY inst_id ORDER BY oi_usd) AS pctile
  FROM oi_funding
  WHERE granularity='1h' AND ts > now() - INTERVAL 90 DAY
)
SELECT inst_id, MAX(pctile) FILTER (WHERE oi_usd = (
  SELECT oi_usd FROM oi_funding o2
  WHERE o2.inst_id = hist.inst_id ORDER BY ts DESC LIMIT 1))
FROM hist GROUP BY inst_id;
```

### 事件传播广度
```sql
SELECT e.event_id, e.canonical_title, e.first_seen_ts, e.coins,
       COUNT(DISTINCT s.source) AS breadth,
       MIN(s.seen_ts) AS first_source_ts
FROM events e JOIN sightings s USING (event_id)
WHERE e.first_seen_ts > now() - INTERVAL 48 HOUR
GROUP BY 1,2,3,4
ORDER BY breadth DESC, first_seen_ts DESC;
```

### 未来宏观/财报事件
```sql
-- 未来 FOMC 会议
SELECT event_date, detail, has_press_conf FROM macro_calendar
WHERE event_type='FOMC' AND event_date >= today() ORDER BY event_date;

-- 未来加密股财报
SELECT ticker, company, event_datetime, fiscal_period FROM corp_events
WHERE event_datetime >= now() ORDER BY event_datetime;
```

### 采集健康（分析前先自查）
```sql
-- 每个采集器最近一次 ok
SELECT collector, MAX(started_at) FILTER (WHERE status='ok') AS last_ok,
       MAX(started_at) AS last_run
FROM collector_runs GROUP BY 1 ORDER BY 1;
```
> **分析前务必查这个**。如果某源 last_ok 很旧，说明"没数据"可能是采集器坏了（§P1 沉默故障），
> 而不是市场平静。基于坏数据的分析比没有分析更危险。

## 边界

- 本 skill **只讲怎么取**。分类/打分/判断分别是 news-triage、priced-in-check、verdict-write 的职责。
- 永远 `read_only=True`。
- 时间统一 UTC。DuckDB `now()` 是 tz-aware，和 UTC-naive 列比较可能差时区——需要精确时先在 Python 用 UTC naive 传参。
