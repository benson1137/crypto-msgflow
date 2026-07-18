---
name: priced-in-check
description: 把 OI/funding 分位 + breadth 翻译成"已 price in 的程度"。只算拥挤度，不说该不该做。
---

# priced-in-check

回答一个问题：**这个消息/行情，市场是不是已经反应过了？**

边界：**只算"拥挤度/已消化程度"，绝不说"该不该做多/做空"**。输出是"拥挤度画像"，交易决策是人的事。

依赖 [[research-db]]。

## 三个输入维度

| 维度 | 数据源 | 含义 |
|---|---|---|
| **OI 分位** | `oi_funding.oi_usd` | 持仓拥挤度。高分位 = 大家都进场了 = 后续燃料少 |
| **funding 分位** | `oi_funding.funding_rate` | 多空成本失衡。极高正 funding = 多头拥挤、代价高 |
| **breadth** | `sightings` COUNT(DISTINCT source) | 消息传播广度。广 = 已扩散 = 可能已 priced in |

三者共同指向一个直觉：**越拥挤 + 越多人已经在说 = 这消息越可能已经 price in**。

## OI / funding 分位（核心查询）

分位在**这一层算**，不在采集器算（采集器是哑的）。

**关键：OI 和 funding 分位读不同的粒度层**，因为上游窗口不同：
- **OI 分位 → `granularity='1d'`**（`okx_oi_1d` 骨架，~180 天）。这是唯一够 90 日窗口的层。1h 只有 30 天，太短。OI 缓变，日线快照是统计上合理的分位基线。
- **funding 分位 → `granularity='1h'`**（`okx_oi_1h` 骨架，~30 天）。funding 历史端点只有 ~33 天，没有 1d 层。funding 有界（OKX ±0.375%），30 天窗够反映近期失衡。

```sql
-- OI 分位：1d 样本（180 天基底），当前值用 rt15 实时定位
WITH oi_hist AS (
  SELECT oi_usd FROM oi_funding
  WHERE inst_id = 'BTC-USDT-SWAP' AND granularity = '1d'
    AND oi_usd IS NOT NULL
    AND ts > ? - INTERVAL 90 DAY          -- 绑定 utcnow()；可扩到 180
),
oi_now AS (   -- 最新实时值，跨所有粒度取最近一条
  SELECT oi_usd FROM oi_funding
  WHERE inst_id = 'BTC-USDT-SWAP' AND oi_usd IS NOT NULL
  ORDER BY ts DESC LIMIT 1
),
fr_hist AS (  -- funding 分位：1h 层（30 天），forward-fill 后密集
  SELECT funding_rate FROM oi_funding
  WHERE inst_id = 'BTC-USDT-SWAP' AND granularity = '1h'
    AND funding_rate IS NOT NULL
),
fr_now AS (
  SELECT funding_rate FROM oi_funding
  WHERE inst_id = 'BTC-USDT-SWAP' AND granularity = '1h' AND funding_rate IS NOT NULL
  ORDER BY ts DESC LIMIT 1
)
SELECT
  (SELECT oi_usd FROM oi_now)                                        AS oi_now,
  (SELECT COUNT(*) FROM oi_hist WHERE oi_usd <= (SELECT oi_usd FROM oi_now))
    * 1.0 / NULLIF((SELECT COUNT(*) FROM oi_hist), 0)                AS oi_pctile,
  (SELECT funding_rate FROM fr_now)                                  AS funding_now,
  (SELECT COUNT(*) FROM fr_hist WHERE funding_rate <= (SELECT funding_rate FROM fr_now))
    * 1.0 / NULLIF((SELECT COUNT(*) FROM fr_hist), 0)                AS funding_pctile
;
```

> **数据充足性检查**：
> - OI 分位需 1d 样本 ≥ 60 条才可信（越接近 180 越好）；不足时标 "样本不足 [LOW]"。
> - funding 分位需 1h 样本 ≥ 200 条（~8 天）。
> - `NULLIF(...,0)` 防样本为 0 时除零。别拿几条数据算出的 "100% 分位" 当真。

## 翻译成"已 priced in 程度"（这是产出）

| OI 分位 | funding 分位 | breadth | 拥挤度画像 |
|---|---|---|---|
| 高(>0.8) | 高(>0.8) | 广 | **高度拥挤，大概率已 priced in**。追进去是接盘 [INFERRED] |
| 低(<0.3) | 中性 | 窄(1-2) | 未拥挤 + 未扩散，可能是早期 [INFERRED] |
| 高 | 低/负 | 广 | 持仓重但 funding 不贵 → 分歧大，未必 priced in |

**这张表是 [FRAME]——拥挤度是一个符号框架，不等于价格预测。** 高分位不"预测"下跌，只描述"燃料状态"。把框架当预测就是 §CLAUDE.md 禁止的"框架翻译成现实"。

## 宏观 priced-in（净流动性 / PCE / FOMC）

宏观事件的 priced-in 看**日历距离**：

```sql
-- 下一次 FOMC 距今多远（越近，议息结果越未定价）
SELECT event_date, has_press_conf, event_date - (now() AT TIME ZONE 'UTC')::DATE AS days_until
FROM macro_calendar
WHERE event_type='FOMC' AND event_date > (now() AT TIME ZONE 'UTC')::DATE
ORDER BY event_date LIMIT 1;
```

净流动性趋势见 [[research-db]] 的单位换算（WALCL/WTREGEN 百万、RRP 十亿，差 1000 倍）。

## 输出格式

输出**拥挤度画像**：`oi_pctile`、`funding_pctile`、`breadth`、一句话定性（"高度拥挤/未扩散/分歧"）。这三个数值正是 `verdicts` 表要快照的字段——交给 [[verdict-write]] 落库。

**不输出**："该做多/做空"、"利好/利空定价"。

## 反谄媚红旗

- 样本不足却报出漂亮的整数分位（100%/0%）→ 标 [LOW]，说样本不足。
- 三个维度不一致时（OI 高但 funding 负），不要强行编一个自洽故事——如实说"信号冲突"。
