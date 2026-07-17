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

分位在**这一层算**，不在采集器算（采集器是哑的）。90 日窗口：

```sql
-- OI 与 funding 的 90 日分位（当前值在历史里排第几）
WITH hist AS (
  SELECT oi_usd, funding_rate
  FROM oi_funding
  WHERE inst_id = 'BTC-USDT-SWAP'
    AND granularity = '1h'
    AND ts > (now() AT TIME ZONE 'UTC') - INTERVAL 90 DAY
),
latest AS (
  SELECT oi_usd, funding_rate
  FROM oi_funding
  WHERE inst_id = 'BTC-USDT-SWAP' AND granularity = '1h'
  ORDER BY ts DESC LIMIT 1
)
SELECT
  (SELECT oi_usd FROM latest)                                       AS oi_now,
  (SELECT COUNT(*) FROM hist WHERE oi_usd <= (SELECT oi_usd FROM latest))
    * 1.0 / (SELECT COUNT(*) FROM hist)                             AS oi_pctile,
  (SELECT funding_rate FROM latest)                                 AS funding_now,
  (SELECT COUNT(*) FROM hist WHERE funding_rate <= (SELECT funding_rate FROM latest))
    * 1.0 / (SELECT COUNT(*) FROM hist)                             AS funding_pctile
;
```

> **数据充足性检查**：`oi_funding` 攒够 90 天前，分位不可信。样本 < 200 条 1h 数据时，明确标注"分位样本不足，仅供参考 [LOW]"。别拿 8 条数据算出的 "100% 分位" 当真。

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
