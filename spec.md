# crypto-msgflow — 规格 (spec)

> 采集层规格。基于代码事实生成，非设计愿景。
> 运行环境：Python 3.11+ · DuckDB · Linux（数据中心 IP，部分源需 mihomo 代理）

## 0. 设计原则

| # | 原则 | 落地点 |
|---|---|---|
| P1 | 首要敌人是"沉默"不是"崩溃"，没数据 ≠ 成功 | `collector_runs.status` 区分 ok/empty/error/stale |
| P2 | 稀缺资源是"不可重取的时刻" | OKX OI、X 推文、判断记录为 P0 |
| P3 | 采集不发生在 agent 的 turn 里 | cron 哑进程，从不调 LLM |
| P4 | 去重结构必须在采集时建立 | `content_hash` + `first_seen_ts` |
| P5 | 最该存的是判断 | `verdicts` 表 |

## 1. 数据模型（10 张表）

### 1.1 `macro_series` — 宏观时序
```sql
series_id VARCHAR, obs_date DATE, value DOUBLE,
source VARCHAR, fetched_at TIMESTAMP,
PRIMARY KEY (series_id, obs_date, source)
```
`source` 进主键：同一指标可多源并存（如 TGA 有 fred 周均 + treasury 日频），下游自选。

当前入库：fred(7 series) · bls(5) · bea(2) · treasury_fiscal(1)。

### 1.2 `oi_funding` — 拥挤度（P0）
```sql
inst_id VARCHAR, ts TIMESTAMP, granularity VARCHAR,
oi_ccy DOUBLE, oi_usd DOUBLE, funding_rate DOUBLE, mark_price DOUBLE,
fetched_at TIMESTAMP, PRIMARY KEY (inst_id, ts, granularity)
```

### 1.3 `events` / `sightings` — 事件归并
```sql
events(event_id PK = content_hash, canonical_title, first_seen_ts,
       category, coins VARCHAR[])
sightings(event_id, source, seen_ts, url, raw_title,
          PRIMARY KEY (event_id, source, seen_ts))
```
传播广度 = `COUNT(DISTINCT source)`。category：news / macro / social。

### 1.4 `corp_events` — 加密股财报日历（前瞻）
```sql
entity_id, ticker, company, category, event_datetime TIMESTAMP,
title, fiscal_year, fiscal_period, updated_at, fetched_at,
PRIMARY KEY (entity_id, category, event_datetime)
```
category：earnings-call / conference-call。event_datetime 是未来时刻。

### 1.5 `price_candles` — 价格历史（verdict 回填用）
```sql
inst_id, ts TIMESTAMP, bar, open, high, low, close, vol, fetched_at,
PRIMARY KEY (inst_id, ts, bar)
```
1H OHLCV，45 天可回溯窗口。oi_funding 没有连续价格序列，verdict 的
realized_ret 靠这张表对任意历史 ts 取价。

### 1.6 `macro_calendar` — 前瞻宏观日历
```sql
event_type, event_date DATE, detail, has_press_conf BOOLEAN,
source, fetched_at, PRIMARY KEY (event_type, event_date, source)
```
当前只有 FOMC：event_date = 会议末日（声明发布日）。

### 1.7 `verdicts` — 判断记录（最高优先级）
含 priced-in 快照（oi_pctile / funding_pctile / breadth）、可证伪预测
（predicted_dir / window / magn）、回填字段（realized_ret / realized_at）。

### 1.8 `news_fulltext` — 7 天 LRU 缓存
按需 fetch（`collectors.fulltext.get_fulltext`），非采集器写。日 cron evict。

### 1.9 `collector_runs` — 健康日志
```sql
collector, started_at, finished_at, status, rows_written,
max_data_ts, error_type, error_msg, PRIMARY KEY (collector, started_at)
```
status：`ok` | `empty` | `error` | `stale` | `running`。

## 2. 采集器规格（11 个 + 1 watcher）

| 采集器 | schedule | 写入 | 代理 | staleness |
|---|---|---|---|---|
| `fred` | `0 13 * * *` | macro_series | 直连 | 数据ts, 8d |
| `tga` | `0 22 * * 1-5` | macro_series | 直连 | 数据ts, 4d |
| `bls` | `0 13 * * *` | macro_series | mihomo+重试 | 心跳 |
| `bea` | `0 14 * * *` | macro_series | 直连 | 心跳 |
| `okx_oi` | `*/15 * * * *` | oi_funding (rt15) | mihomo | 数据ts, 45m |
| `okx_oi_1h` | `30 */12 * * *` | oi_funding (1h) | mihomo | 数据ts, 3h |
| `okx_price` | `20 */12 * * *` | price_candles | mihomo | 数据ts, 14h |
| `rss` | `*/10 * * * *` | events/sightings | mihomo | 心跳 |
| `x_kol` | `*/10 * * * *` | events/sightings | mihomo | 心跳 |
| `bigdata` | `0 1 * * *` | corp_events | mihomo | 心跳 |
| `fomc` | `0 12 * * 1` | macro_calendar | mihomo | 心跳 |
| `listing_alert`(watcher) | 5s 轮询 | Lark 告警群 | mihomo | — |

**OKX OI 双层**：`okx_oi` 每 15 分钟存实时快照（granularity=`rt15`，best-effort 纹理，
non-recoverable）；`okx_oi_1h` 一天两次拉 30 天 OI 历史窗（granularity=`1h`，DELETE+INSERT
重刷 → 断线自愈可回补）。分析层读 1h 骨架。

**staleness 两种语义**：
- **数据ts**：节奏源（OKX 定频、FRED/TGA 定期发布），最新数据太旧 = 采集器坏了。
- **心跳**：内容驱动源（X/RSS）或 obs_date 结构性滞后源（BLS/BEA 月度数据延迟 4-6 周发布）。活性靠 `collector_runs` 心跳 + 连续 empty 检测（仅节奏源），不用数据 ts。

## 3. 各源 series/标的

| 源 | 内容 |
|---|---|
| fred | WALCL, WTREGEN, RRPONTSYD, DGS10, T10YIE, DFII10, BAMLH0A0HYM2(HY OAS) |
| bls | CPI(CUUR0000SA0), Core CPI, 失业率, NFP, 时薪 |
| bea | PCEPI(headline), PCEPILFE(core) — NIPA T20804 |
| tga | 财政部日频 TGA 余额 |
| okx_oi / okx_price | BTC-USDT-SWAP, ETH-USDT-SWAP（OI/funding + 1H OHLCV） |
| bigdata | COIN, MSTR, MARA, RIOT, HOOD, CLSK 财报 |
| fomc | FOMC 会议日程 + 声明 RSS |

## 4. 健康监控（不可裁剪）

每采集器三断言：**存在性 · 契约 · 时效性**。第三条最关键——防"接口 200 但返回旧缓存"。
连续 empty 检测（6h 内 ≥12 次全空 → 告警）。心跳（最近一次 ok 超阈值 → 告警）。

## 5. 已知边界

- **精确 hash 去重**：抓不住改写标题的转载。v0.1 接受（宁可拆成两个也不误并）。
- **BEA/BLS obs_date 滞后**：月度数据发布延迟，用心跳判活性。
- **HTTP 200 陷阱**：BEA 未激活 key 也返 200，错误藏 body — 代码显式检查 `Results.Error`。
