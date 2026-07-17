# crypto-msgflow

加密货币消息面采集器 — append-only 数据管道。

## 设计原则

1. **首要敌人是"沉默"，不是"崩溃"** — 任何采集器，没有数据 ≠ 成功
2. **存储的稀缺资源是"不可重取的时刻"** — OI history、推文、你的判断记录
3. **采集不发生在 agent 的 turn 里** — 采集器是 cron 下的哑进程，从不调用 LLM
4. **去重结构必须在采集时建立** — `first_seen_ts` 和传播广度事后重建不出来
5. **最该存的是判断，不是数据** — `verdicts` 表是整个系统的产出

## 架构

```
collectors/ (cron)  →  research.db (DuckDB)  →  Claude Code skills (分析层)
watchers/ (独立进程) →  Telegram (低延迟告警)
```

## 快速开始

```bash
# 安装依赖
uv pip install -e .

# 配置
cp config/example.toml config/secrets.toml
# 编辑 secrets.toml 填入 API keys

# 初始化数据库
python scripts/init_db.py

# 运行单个采集器（测试）
python -m collectors.fred

# 部署 cron（生产）
# 见 scripts/deploy_cron.sh
```

## 数据模型

| 表 | 用途 | 优先级 |
|---|---|---|
| `verdicts` | 判断记录 + 回测 | **P0** |
| `oi_funding` | 拥挤度（不可重取） | **P0** |
| `events` / `sightings` | 事件去重 + 传播广度 | 核心 |
| `macro_series` | 宏观时序 | 基础 |
| `news_fulltext` | 7天LRU缓存 | 辅助 |
| `collector_runs` | 健康日志 | 监控 |

## 开发顺序

文档 §8 的刻意反直觉顺序：

1. ✅ 项目骨架
2. `verdicts` 表 + 回填脚本（先能记录判断）
3. `BaseCollector` + 健康日志 + 告警
4. `okx_oi.py` + gap 回补（P0：每晚一天是永久损失）
5. `events`/`sightings` + 去重逻辑
6. `fred.py` + `tga.py`
7. smoke tests（不许跳过）
8. `rss.py`
9. `x_kol.py`
10. `watchers/listing_alert.py`

## License

Private use only.
