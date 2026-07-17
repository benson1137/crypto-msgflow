---
name: verdict-write
description: 写 verdicts 表的规程——必填字段、post-hoc 自查、可证伪性检查。强制产出可证伪的预测。
---

# verdict-write

`verdicts` 是整个系统的**产出**（spec §P5）。原始数据可再生，判断记录不可。这个 skill 是写入规程。

依赖 [[research-db]]、[[news-triage]]、[[priced-in-check]]（判断的输入来自它们）。

## 铁律：强制可证伪

写 verdict 时**必须**产出一个能被未来价格证伪的预测，否则就是事后解释。三件套缺一不可：

| 字段 | 含义 | 例 |
|---|---|---|
| `predicted_dir` | 方向 | `up` / `down` / `none` |
| `predicted_window` | 时间窗 | `4h` / `24h` / `7d` |
| `predicted_magn` | 幅度 | `0.03`（3%） |

没有这三个，`realized_ret` 回填就无从对比——你就永远在写"事后看当然是这样"。

## 写入命令（真实 CLI）

```bash
python3 scripts/create_verdict.py "<claim>" \
  --label INFERRED --confidence MED \
  --event-id <event_id> \
  --oi-pctile 0.85 --funding-pctile 0.90 --breadth 12 \
  --predict-dir down --predict-window 24h --predict-magn 0.03
```

参数口径（与 `create_verdict.py` 一致）：

- `claim`（位置参数，必填）：判断内容
- `--label`：`KNOWN|COMPUTED|INFERRED|COMMON|FRAME|GUESS`（必填）
- `--confidence`：`HIGH|MED|LOW|VERY_LOW|UNKNOWN`（必填）
- `--event-id`：关联的 events.event_id（可选但强烈建议）
- `--oi-pctile / --funding-pctile / --breadth`：priced-in 快照（来自 [[priced-in-check]]）
- `--predict-dir / --predict-window / --predict-magn`：可证伪预测三件套
- `--post-hoc`：标记为事后解释（见下）

## post-hoc 自查（写入前必答）

问自己一句：**如果我事先不知道结果，这个框架能预测这件事吗？**

- 能 → 正常 verdict，填预测三件套
- 不能，只能解释已发生的 → **必须加 `--post-hoc`**，且 label 最高 `INFERRED`

这对应 CLAUDE.md 的 `[INFERRED, post-hoc]` 规则。post-hoc 的判断不进命中率统计，但要如实记录——它暴露的是"这个框架只能解释、不能预测"。

## label / confidence 天花板

- `FRAME`（如拥挤度、占星式符号体系）对现实的判断 → confidence 最高 `LOW`
- `GUESS` → 最高 `LOW`
- 只有 `KNOWN`/`COMPUTED` 且有多源/计算支撑，才够 `HIGH`

拥挤度画像来自 [[priced-in-check]]，本质是 [FRAME]。基于它写方向预测时，label 该是 `INFERRED`，confidence 别超 `MED`——拥挤不"预测"下跌。

## 回填（唯一允许 UPDATE 的地方）

预测窗口到期后，回填真实收益：

```bash
python3 scripts/backfill_verdicts.py --apply
```

它找 `realized_ret IS NULL` 且 `ts + window` 已过的 verdict，算真实收益写回。回填后可查自己的校准度：

```sql
-- 标 HIGH 的判断，命中率真有 80% 吗
SELECT confidence,
       COUNT(*) AS n,
       AVG(CASE WHEN (predicted_dir='up' AND realized_ret>0)
                  OR (predicted_dir='down' AND realized_ret<0)
                THEN 1.0 ELSE 0.0 END) AS hit_rate
FROM verdicts
WHERE realized_ret IS NOT NULL AND post_hoc = FALSE
GROUP BY confidence;
```

这条查询是整个系统存在的理由：**它让你发现自己标 HIGH 的判断实际命中率是不是 80%**。

## 反谄媚红旗（写入前扫一遍）

- 预测三件套缺失或含糊（"可能会波动"）→ 不可证伪，重写或标 post-hoc。
- claim 漂亮得不像话、一个框架解释一切 → 降 confidence、补 [GUESS]。
- 为保持前后一致而坚持旧判断 → 公开修正，别硬撑。
