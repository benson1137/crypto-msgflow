---
name: news-triage
description: 对 events/sightings 里的新闻做分类、抽币种、可交易性打分，输出候选列表。只输出候选，不下判断。
---

# news-triage

把原始新闻流筛成"值得看的候选"。**边界：只排序、只打分、只输出候选，绝不下"该不该做"的判断**——那是人（或 verdict-write）的事。

依赖 [[research-db]]：所有取数口径以它为准。

## 职责链

```
events + sightings  →  分类  →  抽币种  →  可交易性打分  →  候选列表（排序）
```

## 1. 数据从哪来

新闻已经在采集时归并进 `events` + `sightings`（见 research-db）。你**不重新抓数据**，只查库。

- `events.category` 采集时已粗分，实际只有三个值：`news` / `macro` / `social`
- `events.coins` 采集时已抽好（`extract_coins()`），是 `VARCHAR[]`
- `sightings` 一行一个来源，`COUNT(DISTINCT source)` = breadth（传播广度）

## 2. 分类（category 已有，按需细化）

采集层给的是粗类。如果要更细的可交易分类，在这一层做，**不要回写 events**（events 是 append-only 原始层）。建议映射：

| 细类 | 判据（标题关键词/来源） | 可交易性 |
|---|---|---|
| listing | "will list" / "上线" / okx_news 来源 | 高（有明确标的） |
| regulation | "SEC" / "MiCA" / "license" | 中（影响面广但慢） |
| hack | "exploit" / "hack" / "drained" | 高（快速定向） |
| macro | category='macro'（FOMC 等） | 高（but 全市场） |
| social | category='social'（x_kol） | 低（需 breadth 佐证） |

**这是启发式，标 [INFERRED]，不是真理。**

## 3. 抽币种（已在采集层完成）

`events.coins` 已经是抽好的数组。口径（来自 `collectors/dedup.py`，勿重复实现）：

- 匹配 `$BTC` 和裸 `BTC`（2-10 位大写）
- 别名归一：`BITCOIN→BTC`、`ETHEREUM→ETH`、`TETHER→USDT`
- stopwords 过滤：`USD US UK EU SEC CEO IPO ETF AI NFT API`

**已知局限**：首字母大写的 "Bitcoin" 抽不到（只认全大写或 $ 前缀）。v0.1 接受。查询时若发现 `coins=[]` 但标题明显含币种，是这个局限，不是 bug。

## 4. 可交易性打分（本 skill 的核心产出）

打分是**相对排序**，不是绝对分。建议因子：

```sql
-- 候选列表：近 24h 事件，按 breadth × 新鲜度排序
SELECT
  e.event_id,
  e.canonical_title,
  e.category,
  e.coins,
  e.first_seen_ts,
  COUNT(DISTINCT s.source)            AS breadth,       -- 多少家在说
  MIN(s.seen_ts)                      AS first_source_ts,
  (now() AT TIME ZONE 'UTC') - e.first_seen_ts AS age
FROM events e
JOIN sightings s USING (event_id)
WHERE e.first_seen_ts > (now() AT TIME ZONE 'UTC') - INTERVAL 24 HOUR
GROUP BY 1,2,3,4,5
ORDER BY breadth DESC, e.first_seen_ts DESC;
```

**打分原则**：
- **breadth 高 = 已在扩散**。但注意——breadth 高既可能是"重要"，也可能是"已经 priced in"。把它交给 [[priced-in-check]] 判断，本 skill 只报 breadth 数值。
- **breadth=1 + 新 = 可能是独家/早期**，也可能是噪音。不替用户决定。
- **有明确 coins = 更可交易**（能对应到标的）。

## 5. 输出格式

输出**候选列表**，每条带：`event_id`、标题、coins、category、breadth、first_seen_ts。**不带**"建议买/卖"、"利好/利空"——越界了。

## 反谄媚红旗

- 如果所有候选都指向同一结论（"全是利好"），停下——大概率是你在硬凑叙事。
- breadth=1 的事件不要包装成"重大独家"。标 [GUESS]。
- 没有可交易候选时，就说"今天没有值得看的候选"，不要为了有输出而拔高噪音。
