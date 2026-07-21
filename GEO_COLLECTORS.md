# 地缘 Collectors 实现摘要

2026-07-21 实现。规格基于你发的附件《crypto-msgflow 地缘 collector 取数规格》，优先级 Polymarket → OFAC → GDELT。

---

## 已交付（可实跑验证）

### 1. **Polymarket** — 预测市场隐含概率

- **collectors/polymarket.py** — Gamma API `/events` → `polymarket_snapshots` 表
- **快照式**：每 10 分钟拉一批，ts = 入库时刻（point-in-time），一次 ~1600 行 / 791 市场
- **tag 过滤精准**：crypto=21, economy=100328, fomc=100478, fed=159, fed-rates=100196, economic-policy=101800。验证：最高量市场是 Fed 利率决议（92.65% no-change），没有体育娱乐噪声。
- **TLS / proxy**：这台机器 direct 全挂，走 mihomo（use_env_proxy=True）。curl_cffi（impersonate=chrome）主路径 + httpx 兜底，都骑代理。
- **信号**：implied_prob 的**跳变**（prediction 先于新闻）
- **cron**：`*/10 * * * *`，已加到 crontab.example
- **smoke test**：30 分钟新鲜度 + prob ∈ [0,1] 校验，通过

### 2. **OFAC 地址黑名单（SDN § 3b）** — 制裁加密地址

- **collectors/ofac_addresses.py** — 0xB10C GitHub raw txt → `ofac_crypto_addresses` 表
- **append-only 信号**：`first_seen` = 我方首次见到该 (address, symbol) 的时刻 → 新制裁落地
- **首跑结果**：951 地址（XBT 522, TRX 188, ETH 96, USDT 93, ...）
- **幂等验证**：二次跑 0 new，status=`empty`（正确行为，今天没新制裁）
- **18 个币种**：ARB/BCH/BSC/BSV/BTG/DASH/ETC/ETH/LTC/SOL/TRX/USDC/USDT/XBT/XMR/XRP/XVG/ZEC
- **信号硬度**：规格评为三条里"信号最硬"（混币器/交易所被制裁 = 板块即时反应）
- **cron**：`40 0 * * *`（daily，0xB10C 在 0 UTC 刷新），已加
- **smoke test**：fetched_at 3 天新鲜度（liveness），通过

### 3. **GDELT** — 宏观/地缘报道量 + 情绪基调

- **collectors/gdelt.py** — BigQuery `gkg_partitioned` → `gdelt_tone` 表
- **待凭证，代码齐全**：没 `gdelt.bq_project` 时**响亮失败**（SchemaDrift + exit 1 + alert），不假装成功
- **验证通过**：无凭证跑，按设计报 `"gdelt.bq_project not set — GDELT needs a BigQuery billing project + credentials. Configure it before enabling this cron."`，collector_runs 记录 status=`error` / error_type=`schema_drift`
- **hourly 聚合**：ts_hour / theme_bucket (rates/centralbank/sanctions/conflict) / doc_count / avg_tone
- **成本守卫**：WHERE `_PARTITIONTIME >= ...` 卡 90 分钟窗口，省 BQ 扫描（1TB/月免费额度）
- **信号定位**（规格）：**不是加密新闻源**（GDELT 对币圈覆盖薄且噪），是 risk-on/off 的**宏观背景特征** — doc_count 突增 + avg_tone 骤降 = risk-off 压力（软领先量，当特征用不当触发器）
- **cron**：`5 * * * *` 在 crontab.example 里**注释掉**，待凭证启用
- **依赖**：`pip install 'crypto-msgflow[gdelt]'` 装 `google-cloud-bigquery`（optional extra，不污染 base）
- **GDELT 能跑的必要前置**：
  1. GCP 项目（billing）+ BigQuery API 启用
  2. Service Account JSON 或 ADC，设到 `GOOGLE_APPLICATION_CREDENTIALS` env 或 `gdelt.bq_credentials_json` config
  3. 装 SDK：`pip install 'crypto-msgflow[gdelt]'`
  4. secrets.toml: `bq_project = "your-gcp-project"`
  5. 取消 crontab 里那行注释

---

## 未做（按规格划定 scope）

### OFAC 事件流（§ 3a）— 制裁落地新闻

**不做**，留给下轮：
- RSS 已于 2025-01-31 正式退役（本轮核实），不是"URL 没找到"，是没了
- recent-actions 页是 HTML 抓，脆；Federal Register JSON API 是干净选项，但需新适配
- 相比 3b 地址黑名单（硬信号），3a 事件流价值递减
- **建议**：用 Federal Register 的 OFAC 关键词 feed（结构规整），作为 rss.py 的又一条源（category='sanctions'），写 events/sightings 就行，不需要单独 collector

### UCDP / cyber-IOC

规格已决定**不接**：UCDP 与加密资产相关性太弱；cyber-IOC 延迟高 + 覆盖不对口

---

## 变更清单

### 新增文件
- `collectors/polymarket.py` — Polymarket collector
- `collectors/ofac_addresses.py` — OFAC 地址 collector
- `collectors/gdelt.py` — GDELT collector（待凭证）
- `logs/{polymarket,ofac_addresses,gdelt}.log` — 日志位

### 表 schema（scripts/init_db.py）
```sql
-- § 1.10
CREATE TABLE polymarket_snapshots (
  ts TIMESTAMP, market_slug VARCHAR, outcome VARCHAR, implied_prob DOUBLE,
  volume24hr DOUBLE, question VARCHAR, event_slug VARCHAR, end_date VARCHAR,
  fetched_at TIMESTAMP,
  PRIMARY KEY (ts, market_slug, outcome)
);

-- § 1.11
CREATE TABLE ofac_crypto_addresses (
  address VARCHAR, symbol VARCHAR, first_seen TIMESTAMP, fetched_at TIMESTAMP,
  PRIMARY KEY (address, symbol)
);

-- § 1.12
CREATE TABLE gdelt_tone (
  ts_hour TIMESTAMP, theme_bucket VARCHAR, doc_count INTEGER,
  avg_tone DOUBLE, fetched_at TIMESTAMP,
  PRIMARY KEY (ts_hour, theme_bucket)
);
```

### 配置（collectors/config.py + config/*.toml）
```toml
[polymarket]
tag_ids = [21, 100328, 100478, 159, 100196, 101800]
min_volume24hr = 5000.0
per_tag_limit = 100

[ofac]
raw_base = "https://raw.githubusercontent.com/0xB10C/ofac-sanctioned-digital-currency-addresses/lists"
symbols = ["ARB","BCH","BSC",...18个]

[gdelt]
bq_project = ""
bq_credentials_json = ""
lookback_minutes = 90
```

### 依赖（pyproject.toml）
```toml
[project.optional-dependencies]
gdelt = ["google-cloud-bigquery>=3.0.0"]
```

### Cron（scripts/crontab.example）
```cron
*/10 * * * * flock ... collectors.polymarket
40 0 * * *   flock ... collectors.ofac_addresses
# 5 * * * * flock ... collectors.gdelt  # 待 bq_project
```

### 测试（tests/smoke/test_health.py）
- `test_polymarket_snapshots()` — 30min 新鲜度 + prob ∈ [0,1]
- `test_ofac_addresses()` — fetched_at 3d 新鲜度
- `test_consecutive_empty()` — 加 `ofac_addresses` 到 content_driven 白名单

---

## 实测结果（2026-07-21）

```bash
# polymarket
✓ polymarket: wrote 1582 rows
total snapshots: 1582 / distinct markets: 791
top by vol: Fed-decrease-25bps (No: 99.65%), Fed-no-change (Yes: 92.65%)

# ofac_addresses
✓ ofac_addresses: wrote 951 rows  # 首跑
⚠️  ofac_addresses: no new data (this is OK if expected)  # 二次跑（幂等）
breakdown: XBT 522, TRX 188, ETH 96, USDT 93, ...

# gdelt (no creds)
🚨 SchemaDrift: gdelt.bq_project not set — GDELT needs a BigQuery billing
   project + credentials. Configure it before enabling this cron.
exit=1, collector_runs: status=error / error_type=schema_drift
```

**smoke tests**: 新增的 2 个 test 全 pass，6 个既有失败（FRED/TGA/OKX 数据停在 07-18）与本轮无关。

---

## 与原规格的差异/修正

1. **OFAC RSS 退役**（2025-01-31）— 本轮核实。规格写的"URL 没核到"已过时，是彻底没了。
2. **Polymarket JA3** — 不是"被 JA3 硬墙"，是"mihomo 出口 + Cloudflare 间歇性拦"。这台机器 direct 全失败，只有代理通。curl_cffi 补 TLS 指纹，但**出口 IP 信誉/区域**才是能不能过的关键变量。
3. **GDELT 分区列** — `_PARTITIONTIME` 验证正确（GDELT 官方公告），模板 SQL 可用。
4. **Polymarket outcomes/outcomePrices** — API 返回的是 **JSON 字符串**（`"[\"Yes\",\"No\"]"`）不是数组，要 `json.loads`。代码已处理。

---

## 下一步（如需）

- [ ] **启用 GDELT**：配置 bq_project + 凭证，装 SDK，取消 cron 注释
- [ ] **OFAC 事件流（§ 3a）**：用 Federal Register API 做成 rss 源（category='sanctions'）
- [ ] **Polymarket 跳变检测**：`implied_prob` diff 超阈值 → alert（现在只存快照，检测逻辑在 verdict/watcher 层）
- [ ] **OFAC 新地址 → 事件流联动**：`new_count > 0` 时调 `record_event` 写一条 "OFAC designated N new addresses (symbols)" 到 events/sightings，自动进 verdict 候选池

---

**总结**：本轮交付 Polymarket（实跑验证 ✓）+ OFAC 地址黑名单（实跑验证 ✓）+ GDELT（代码齐全，待凭证启用）。三条全部走 mihomo 代理（这台机器环境约束）。OFAC 事件流留给下轮用 Federal Register API 做。
