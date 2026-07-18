# crypto-msgflow — 代码架构 (design)

> 基于代码事实。描述模块划分、控制流、扩展点。

## 1. 目录结构

```
collectors/          # cron 哑进程，写 DuckDB
  base.py            # BaseCollector 抽象基类 + SchemaDrift
  config.py          # pydantic 配置模型 + TOML 加载
  timeutil.py        # utcnow() — 全系统统一 UTC-naive
  rate_limit.py      # TokenBucket 令牌桶 + RateLimited
  alerts.py          # Lark (lark-cli bot) / stderr 告警
  dedup.py           # normalize / content_hash / extract_coins
  events.py          # events/sightings 写入辅助
  fulltext.py        # 按需全文 get_fulltext + 7天LRU（curl_cffi）
  fred.py tga.py bls.py bea.py        # 宏观 → macro_series
  okx_oi.py                            # OI 实时(rt15)+1h骨架 → oi_funding
  okx_price.py                         # 1H OHLCV → price_candles（verdict回填源）
  rss.py x_kol.py                      # 新闻/社交 → events/sightings
  bigdata.py                           # 加密股财报 → corp_events
  fomc.py                              # FOMC 日程 → macro_calendar
watchers/
  listing_alert.py   # 常驻进程(systemd)，5s 轮询上币公告 → Lark 告警群
scripts/
  init_db.py           # 建表（幂等 CREATE IF NOT EXISTS）
  create_verdict.py    # 手动写判断
  backfill_verdicts.py # 回填 realized_ret
  crontab.example      # 部署模板
tests/
  smoke/test_health.py # 每采集器三断言
  test_dedup.py        # 去重单元测试
```

## 2. BaseCollector — 核心抽象

所有采集器继承 `BaseCollector`，实现 3 个抽象方法：

```python
class MyCollector(BaseCollector):
    name = "..."                    # collector_runs 主键
    schedule = "..."                # cron 表达式（文档用）
    max_staleness = timedelta(...)
    use_env_proxy = True/False      # 见 §3 代理策略
    staleness_by_data_ts = True/False  # 见 §4 staleness 语义

    def fetch(self) -> list[dict]: ...          # 拉数据
    def validate(self, rows) -> None: ...       # 契约检查，漂移抛 SchemaDrift
    def upsert(self, rows) -> int: ...          # 写 DB，返回行数
```

### run() 控制流（模板方法）

```
run():
  写 collector_runs (status='running')
  try:
    rows = fetch()
    validate(rows)            # 契约不符 → SchemaDrift，硬失败
    n = upsert(rows)
    max_ts = 提取最新数据时间戳（ts 或 obs_date，_to_datetime 归一）
    判定 status:
      n==0                              → empty
      staleness_by_data_ts 且 太旧      → stale + 告警
      else                              → ok
    更新 collector_runs
  except:
    collector_runs status='error' + error_type + 告警
    raise                     # 不吞异常
```

**关键**：`empty` 与 `ok` 分开（P1）；异常不静默（P3 推论）。

## 3. 代理策略（per-source）

数据中心 IP + 全局 mihomo 代理（`HTTP_PROXY` 环境变量）。`make_client()` 按
`use_env_proxy` 决定：

- `use_env_proxy=True` → `trust_env=True`，走 mihomo
- `use_env_proxy=False` → `trust_env=False`，直连

实测结果（反直觉，per-source）：

| 源 | 策略 | 原因 |
|---|---|---|
| FRED / TGA / BEA | 直连 | 美国政府源，走 mihomo 反而断 |
| BLS | mihomo + 重试 | 直连超时；mihomo 出口对 BLS 仅约 25% 健康，短 timeout+6 次重试命中好节点 |
| OKX (oi/price) | mihomo | 数据中心 IP 直连被墙 |
| RSS / FOMC / BigData / X | mihomo | 可通 |

## 4. staleness 两种语义

`base.py` 的 `staleness_by_data_ts` 开关区分：

- **True（数据ts）**：节奏源。最新数据超 `max_staleness` = 采集器坏了 → stale + 告警。
  用于 okx_oi(45m)、okx_oi_1h(3h)、okx_price(14h)、FRED(8d)、TGA(4d)。
- **False（心跳）**：内容驱动源（X/RSS，KOL 沉默几天正常）或 obs_date 结构性滞后源
  （BLS/BEA 月度数据延迟发布）。活性靠 `collector_runs` 心跳 + 连续 empty 检测。

这是踩坑修正：早期 x_kol 用数据 ts 判活性，KOL 一沉默就误报 stale → 告警疲劳。

## 5. 去重（dedup.py）

```
normalize(title)     # 去前缀(breaking/快讯...)、保留中文、collapse 空白
content_hash(title)  # sha256(normalize)[:16] = event_id
extract_coins(title) # 启发式抽 $BTC/BTC/别名，过滤 SEC/ETF 等停用词
```

归并流程（rss.py / x_kol.py 的 upsert）：
1. `content_hash` → event_id
2. events 无此 id → INSERT（`first_seen_ts=now`），有则不动（保留首见时间）
3. 总是 INSERT 一条 sighting（source 区分来源）

## 6. 时间统一（timeutil.py）

服务器是 UTC+8。若混用本地时间，staleness 判定会差 8 小时。全系统统一
`utcnow()` → UTC-naive datetime。DuckDB 查询用绑定参数传 `utcnow()`，
不用 SQL `NOW()`（后者是 tz-aware 本地时，同样错位）。

## 7. 限流（rate_limit.py）

`TokenBucket(rate, capacity)` 每源一个令牌桶，`get_limiter(source, rate_per_min)`
单例。429 时抛 `RateLimited(retry_after)`，由调度层决定，不在采集器内死等。

## 8. 特殊设计：corp_events / macro_calendar 的 upsert

两者是前瞻数据（未来时刻），日程可能被上游修订，所以用 **DELETE+INSERT** 反映更新：
- `bigdata.py`：earnings 按 (entity, fiscal_year, fiscal_period) 去重（datetime 可改）；
  conference-call 无 fiscal，按 PK (entity, category, event_datetime) 去重，
  避免空 fiscal 导致多条塌成一条。
- `fomc.py`：按 (event_type, event_date, source) 去重。

## 9. 扩展新采集器

1. 继承 `BaseCollector`，设 name/schedule/代理/staleness 语义
2. 实现 fetch/validate/upsert
3. 在 `config.py` 加配置模型（如需 key）
4. 在 `tests/smoke/test_health.py` 加三断言
5. 在 `scripts/crontab.example` 加调度行
