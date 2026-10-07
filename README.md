# wxgrid

乡镇级精细化天气预报。**8 家全球模式**（ECMWF IFS / AIFS、DWD ICON、UKMO、CMA GRAPES、CMC GEM、
Météo-France ARPEGE、NOAA AIGFS）按乡镇实际海拔取点、等权平均，再用周边 7 个国家站的实况做**滚动
偏差订正和降水分位数映射**；按北京时分**白天（08—20 时）/ 夜间（20—次日 08 时）**写五天预报，另给
0—72 小时逐3小时预报。GEFS 21 成员的降水概率只放在明细里，不进预报用语。产出文字预报、结构化 JSON、
只读 API 和 Web 页面；页面上公开样本外检验成绩。

取不到多模式数据时自动退回 ECMWF IFS + NOAA GFS 原始 GRIB 方案（本地按递减率降尺度，不做站点订正）。

另有**自主引擎** `--engine native`，**不经 Open-Meteo 或任何第三方服务**：直接读 ECMWF（IFS、AIFS）
与 NOAA（GFS、GEFS）开放数据的原始 GRIB，本地降尺度，用自己存档的历史预报对周边国家站训练加权共识和订正，
数据全部允许商用。产品格式与页面完全相同。方法、回测成绩和切换步骤见下方「自主引擎」。

- 代码：`/opt/wxgrid`
- 数据：`/var/lib/wxgrid`（产品 JSON + GRIB 缓存，不进 git）
- 页面/API：`http://<服务器地址>:8790`（对外开放，见下方「安全」）

## 快速开始

```bash
cd /opt/wxgrid

# 1. 乡镇表（已有 yanshan_townships.csv，可跳过）
python3 -m wxgrid townships --wikidata 铅山县 --out yanshan_townships.csv --dem-cache ./dem-cache

# 2. 打印一份预报（约 10 分钟，联网下载）
python3 -m wxgrid bulletin --townships yanshan_townships.csv \
    --county 铅山县 --seat 河口镇 --days 5

# 3. 产出一份产品到数据目录（Web/API 读它）
python3 -m wxgrid publish --townships yanshan_townships.csv \
    --county 铅山县 --seat 河口镇 --data-dir /var/lib/wxgrid

# 4. 起服务
python3 -m wxgrid serve --host 0.0.0.0 --port 8790 --data-dir /var/lib/wxgrid
```

## 部署

```bash
install -m644 deploy/*.slice deploy/*.service deploy/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now wxgrid-web.service wxgrid-publish.timer
```

- `wxgrid-publish.timer` 每天北京时 **07:30 / 19:30** 各跑一次（各带 ≤10 分钟随机延迟）。
  07:30 发布的产品从**今天白天**开始（10 个时段 = 5 整天），19:30 发布的从**今天夜间**开始
  （今晚 + 5 整天 = 11 个时段）。起报时次取所有源都发布到所需时效的最新一个（通常 07:30 用前一天
  12Z、19:30 用当天 00Z 或 06Z）。publish 先解析时次与首个时段：同一时次、同一首时段、当前格式的
  产品已在盘上就直接返回，不下载；否则（旧格式、缺逐时文件、同一时次换了发布时段）重算。
- `wxgrid-web.service` 常驻，只读 `/var/lib/wxgrid`，不做任何计算。
- 换县：改 `wxgrid-publish.service` 里的 `--townships/--county/--seat`。

### 资源限额

整机 4 核 / 8 GB，目标：wxgrid 全部合计 **CPU ≤ 30%、内存 ≤ 66%**。两个服务都放进
`wxgrid.slice`，由 slice 统一封顶，单元各自再有更紧的上限：

| 单元 | CPUQuota | MemoryMax | Nice | 实测 |
|---|---|---|---|---|
| `wxgrid.slice`（合计） | 120%（=1.2 核 = 30%） | 5.2 GB（66%） | — | 上限，两者叠加也不会超过 |
| publish（每天 2 次） | 120% | 2 GB | 15 | 见下 |
| web（常驻） | 25% | 384 MB | 10 | 空闲接近 0，约 30 MB |

publish 实测（多模式，含逐时和 GEFS 概率）：墙钟约 2 分钟，CPU 约 25 秒；每天第一次还要刷新订正
（约 1 分钟网络、7 秒 CPU、峰值约 200 MB）。退回 GRIB 方案时：墙钟 9–17 分钟，CPU 约 2 分 20 秒，
峰值常驻内存约 580 MB。cgroup 内存读数会更高，因为它把写 GRIB 缓存产生的页缓存也算进去，
那部分内核可随时回收。手动跑任务时也放进同一个 slice，就不会挤占整机：

```bash
systemd-run --scope --slice=wxgrid.slice -p CPUQuota=100% nice -n 15 python3 -m pytest tests -q
```

GRIB 缓存（`$WXGRID_CACHE`，目前只缓存 GFS）超过 36 小时的条目在每次 publish 开始时删除，
盘上通常保持在 1 GB 以内。

两个单元都开了 `ProtectSystem=strict`；publish 仅可写 `/var/lib/wxgrid`，web 对它只读。

## Web 页面

单页、无构建、无 CDN（`wxgrid/web/static/`）。自上而下：

1. 县城：**当前时段与下一时段**（如"今天夜间 最低 22℃ / 明天白天 最高 30℃"），各带天气、风、降水时段；
   旁边是五天概述。时段按浏览时刻滚动：过期的时段自动隐去，过了午夜的夜间写作"今天凌晨"
2. 提示：按预警信号标准推算，明确标注"非气象部门发布"
3. **未来三天 · 逐3小时**：任选乡镇；气温曲线、3 小时降水柱、风向风力、夜间底色；按天跳转；
   展开"逐3小时明细"才看到降水概率
4. **未来五天**：每天一列，上排白天天气、中间最高/最低气温折线、下排夜间天气，底部白天/夜间风
5. 各乡镇（选中那天）：白天、夜间天气，夜间最低～白天最高的气温条（共用色标），风，降水量；
   点一行打开详情：逐3小时图与明细 + 各时段表（含降水概率）
6. 模型与方法：数据源、流程、公式、为什么是逐3小时、用语阈值、局限；折叠的文稿与接口

页面只读 `/api/latest`、`/api/runs`，可在顶栏切换历史起报时次。

## 只读 API

无鉴权，只读，`Access-Control-Allow-Origin: *`。当前绑 `0.0.0.0:8790`，公网可达。
进程跑在 `ProtectSystem=strict` + `ReadOnlyPaths` 下，写不了任何东西，数据目录也无密钥，
所以最坏情况是数据被免费取用，不会被篡改。正式对外建议挂到现有反代加 TLS 与限流。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 状态、已存 run 数、最新 run |
| GET | `/api/latest` | 最新产品全量 JSON |
| GET | `/api/summary` | 仅 meta + 结论 + 每日县级摘要（小体积） |
| GET | `/api/runs` | 历史 run 索引，新→旧 |
| GET | `/api/runs/<file>` | 按索引里的 `file` 取某次 run |
| GET | `/api/townships` | 乡镇名录：id、名称、经纬度、海拔 |
| GET | `/api/forecast/<id 或名称>` | 某乡镇白天/夜间预报，按时段一行（含明细字段 `pop`） |
| GET | `/api/3h/<id 或名称>` | 某乡镇 0—72 小时逐3小时，按行；`?hours=N` 取前 N 小时 |
| GET | `/api/3h` | 全部乡镇逐3小时，按列 |
| GET | `/api/hourly/<id 或名称>` | 逐小时（**参考**，页面不展示）：3 小时值按 GFS 日内形态插值，逐时精度有限 |
| GET | `/api/hourly` | 全部乡镇逐小时（参考），按列 |

以上接口都接受 `?run=<file>`（`/api/runs` 里的 `file`）取历史时次。

```bash
# 河口镇白天/夜间预报、未来 24 小时逐3小时（名称要 URL 编码；也可以用 id，如 Q31855302）
curl -s 'http://localhost:8790/api/forecast/%E6%B2%B3%E5%8F%A3%E9%95%87' | python3 -m json.tool
curl -s 'http://localhost:8790/api/3h/%E6%B2%B3%E5%8F%A3%E9%95%87?hours=24' | python3 -m json.tool
curl -s http://localhost:8790/api/summary | python3 -m json.tool
curl -s http://localhost:8790/api/latest | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["conclusions"]["headline"])'
```

### 产品 JSON 结构（`schema: 2`）

```
meta         schema, county/seat/run/init_time, issue_time(UTC)/issue_local(北京时), member/sources/
             weights/source_label, tz, n_periods, n_townships, series_hours, pop_members/pop_run, generated
periods[]    kind(day|night), name(白天|夜间), date, weekday, rel, label(今天夜间…),
             start_local/end_local, start_lead/end_lead, county{weather,temp_min,temp_max,precip_max,...}
  cells[]    point, weather, temp(白天=最高/夜间=最低), tmax, tmin, tmean, precip, precip_3h_max,
             snow, cloud, wind_text, wind_name, wind_dir, force_lo/force_hi/force_text,
             wind_speed, wind_speed_max, gust, gust_force, windows, windows_text, pop(明细)
days[]       date, weekday, rel, day(periods 下标|null), night(下标|null)
series3h     step_hours=3, times[](北京时，窗口结束时刻), lead_h[],
             points{id:{weather[], temp[], precip[], snow[], cloud[], wind_dir[], wind_name[],
                         wind_force[], wind_speed[], gust[], pop[]}}
townships[]  id, name, lat, lon, elevation, model_elevation
conclusions  headline, alerts[]{type, level, date, detail, criterion}, seat_id
text         与 CLI 一致的文字预报全文（不含概率）
```

`level` 是预报达到的**预警信号标准**的颜色（红/橙/黄/蓝），低于最低标准记"关注"；
这些是按模式推算的提示，不是气象台发布的信号。

### 逐时 JSON 结构（`hourly/<同名文件>.json`）

```
meta      county/seat/run/init_time/tz/tz_label/n_hours/first_lead_h/last_lead_h/
          source/shape(gfs|linear)/method/units/time_convention/pop_members/pop_run/generated
lead_h[]  起报后小时数（3..120）
times[]   北京时 "YYYY-MM-DDTHH:00"，为该小时的**结束**时刻；降水是这一小时内的量
points{id: {name, lat, lon, elevation,
            weather[], temp[], precip[], snow[], cloud[](%), wind_speed[], wind_dir[](来向),
            wind_name[], wind_force[](蒲福), gust[], pop[](所在 6 小时 GEFS 时段)}}
```

`/api/hourly/<乡镇>` 把同一份数据转成按行：`{meta, township, hours:[{time, lead_h, weather, temp, ...}]}`。

提示的判据（对照各预警信号标准）：

| 类型 | 红 | 橙 | 黄 | 蓝 | 关注 |
|---|---|---|---|---|---|
| 暴雨 | 3 h ≥100 mm | 3 h ≥50 | 6 h ≥50 | 12 h ≥50 | 24 h ≥50 |
| 大风 | 平均 ≥12 级或阵风 ≥13 | ≥10 / ≥11 | ≥8 / ≥9 | ≥6 / ≥7 | — |
| 高温 | 日最高 ≥40 ℃ | ≥37 ℃ | 连续三天 ≥35 ℃ | — | ≥35 ℃ |
| 霜冻 | — | — | — | — | 夜间最低 ≤0 ℃ |

## 模型选择与订正（`wxgrid/verify.py`、`wxgrid/postproc.py`）

**怎么选的**：用 OGIMET 转发的 SYNOP 实况（武夷山 58730、邵武 58725、浦城 58731、南城 58715、
景德镇 58527、衢州 58633、南昌 58606，另有庐山 58506 只作高山参考），对 Open-Meteo *Previous Runs*
存档里 11 家模式 60 天的历史预报按 CMA 城镇预报口径打分（白天最高对 12 时报 24 h 最高，夜间最低对 00 时报
24 h 最低，12 小时晴雨 ≥0.1 mm，气温误差 ≤2 ℃ 为准确）。前 30 天拟合、后 30 天评分，结论：

- 单家里 ICON、UKMO、ECMWF IFS 9 km 气温最好；GFS、JMA GSM 在这里明显偏差大，被剔除。
- 8 家平均的气温误差比任何单家都小（第 1 天白天最高 MAE 1.16 → 单家最好约 1.3）。
- 平均把"毛毛雨"放大：晴雨准确率只有 65—68%、预报有雨次数是实况的 3 倍；
  12 小时降水分位数映射后晴雨 89—92%、降水 TS 从约 30 提到 42—56。
- 滚动偏差订正（α = 0.1，按白天/夜间 × 预报第几天，7 站合并）再把白天最高 MAE 降到 0.8—1.1 ℃。
  留一站检验（用其余 6 站的偏差订正第 7 站）同样有效，说明区域偏差能推到县内乡镇。

| 第 1 天（样本外 30 天） | 白天最高 ≤2 ℃ | 夜间最低 ≤2 ℃ | 晴雨 | 降水 TS |
|---|---|---|---|---|
| 本系统：8 家 + 站点订正 | 94% | 98% | 92% | 55 |
| 原方案 ECMWF 0.6 + GFS 0.4 | 70% | 88% | 69% | 36 |
| 单一 ECMWF IFS 9 km | 79% | 90% | 73% | 38 |

**每天怎么更新**：publish 时调用 `postproc.refresh`，一天最多一次：补取各站 SYNOP 和 10 家模式的
previous-runs 存档（保留 62 天，`/var/lib/wxgrid/verify/`），重新拟合 `calibration.json`，
重新做样本外评分写 `scores.json`（产品里的 `verification` 字段、页面"准确性检验"一节）。
刷新失败时继续用 7 天内的上一份订正；更旧就不订正。

```bash
python3 -m wxgrid verify --force        # 立即刷新并打印成绩表
```

数据经 [Open-Meteo](https://open-meteo.com/)（CC BY 4.0）取得：**免费仅限非商业用途**，商业使用需申请 API key
（设 `WXGRID_OPENMETEO_KEY`）。页面页脚已按许可注明来源。每次发布约 1 次预报请求 + 每天约 10 次存档请求，
远低于免费额度；向对方发送的只有乡镇和气象站的公开经纬度、海拔。

## 自主引擎（`--engine native`，不依赖 Open-Meteo）

`wxgrid/sources/raw.py`、`wxgrid/gribbox.py`、`wxgrid/archive.py`、`wxgrid/native.py`、`wxgrid/consensus.py`。

**为什么**：Open-Meteo 免费额度仅限非商业用途；8 家里的 UKMO、GRAPES、GEM、ARPEGE 的原始数据不开放批量下载，
ICON 是三角网格、在这台机器上太重。自主引擎只用生产方直接开放、允许商用的数据，取数 → 降尺度 → 融合 → 订正 → 检验
整条链都在本机，可复现、可审计。

### 数据

| 成员 | 生产方 | 分辨率 · 步长 | 用途 | 许可 · 来源 |
|---|---|---|---|---|
| IFS HRES | ECMWF | 0.25° · 3 h（00/12 UTC 到 144 h） | 共识成员；带 3 h 极值 | 开放数据 CC BY 4.0 · AWS 镜像，退回 data.ecmwf.int |
| AIFS Single | ECMWF | 0.25° · 6 h | 共识成员（AI 模式） | 同上 |
| GFS | NOAA | 0.25° · 3 h | 共识成员；带 6 h 极值 | 公有领域 · NOAA Open Data on AWS |
| GEFS | NOAA | 0.5° · 6 h 降水，21 成员 | 只给逐3小时明细的降水概率 | 同上 |
| SYNOP 实况 | 7 个国家站 | 3 h | 训练与检验 | 经 OGIMET，与多模式方案共用 `verify/obs` |

每个要素按 GRIB 索引按字节范围单独读取、用 ecCodes 解码后**裁到县域周边一小块**（含 7 个国家站，约 15×18 个格点），
单位按每条消息自己的 `units` 换算（IFS 降水是 m、AIFS 是 kg m⁻²；IFS 云量是 0–1、AIFS/GFS 是 %）。
每家每次存一个约 50–160 KB 的 `archive/<模式>/<YYYYMMDDHH>.npz`，保留 75 天（约 60 MB）——这就是训练集：
实况到了以后直接拿当时真正用过的数据训练，训练与预报走同一段代码。

### 流程

1. **选时次**：取 3 家里至少 2 家已发布到所需时效的最新 00/12 UTC 时次（IFS 的 06/18 UTC 只到 90 h）。
   缺一家照常出（权重按比例补足），少于 2 家则退回 GRIB 方案。
2. **降尺度**：双线性插值到乡镇；气温按海拔订正 `T = T格点 + γ (z乡镇 − z模式)`，
   **白天 γ = −6.5 K/km，夜间 γ 取模式自己在当地的递减率**（周边 5×5 格点气温对地形回归，限 −9.8…+4 K/km），
   保留山谷逆温。AIFS 6 小时一步，3 小时值按时间插值、降水均分。
3. **气温共识**：按白天/夜间 × 预报第几天，`T = Σ w_k T_k + a`，`w_k ≥ 0`、`Σ w_k = 1`，
   用最近 60 天站点实况岭回归（向等权收缩）拟合，越近的日子权重越大（半衰期 20 天），`a` 为加权残差均值。
   权重合计为 1，乡镇之间的海拔差不被削弱（自由回归会把高山乡镇往站点平均拉）。白天最高、夜间最低取各家自己的
   时段极值再加权。
4. **降水**：3 家平均的 12 小时雨量按"预报气候 → 实况气候"分位数映射（同多模式方案），时段内 3 小时雨量同比例缩放。
5. **风**：风速取各家风速的平均（不是 u/v 平均后求模），乘以按白天/夜间拟合的"实况均值 / 预报均值"；风向取矢量平均。
   阵风没有检验，不做调整。
6. **降水概率（明细）**：时段概率 = 对 3 家各自分位数映射后的有雨比例与雨量做逻辑回归；逐3小时取 GEFS 所在
   6 小时有雨成员比例。
7. **每天重拟合**（`consensus.refresh`，发布时一天一次）：补 OGIMET 实况 → 从存档重建站点配对 → 拟合 →
   对最近 30 天做样本外回测，写 `native/calibration.json`、`native/scores.json`（页面"准确性检验"一节）。
   刷新失败时沿用 7 天内的上一份；没有可用订正时为等权平均。

### 算法怎么选的：2025 年回测

用 `backfill` 从 AWS 补了 **2025-02-24 至 2025-08-22** 每天 12 UTC 的 IFS、AIFS、GFS、GEFS（AIFS 自 2025-02-25 起），
实况取 NOAA ISD 转发的同一批 SYNOP（开发环境连不上 OGIMET）：有报文原文的取 24 h 极值与 6 h 降水；
经 BUFR 转来的只有 ISD 编码的 12 h 极值，与 3 小时实况比对、偏离不超过 2.5 ℃ 才采用；降水只用有报文原文的站次。
所有方案**在线、样本外**：每次预报只用发布前已结束时段的实况拟合，60 天窗口；7 个低海拔站合并（庐山除外）。
下表由发布用的同一段代码（`consensus.backtest`）算出。

气温（2025-04-03 至 08-23，143 次发布 × 7 站，每格约 930 对；MAE ℃ / 误差 ≤2 ℃ 比例）：

| 方案 | 第1天 白天最高 | 第1天 夜间最低 | 第3天 白天最高 | 第3天 夜间最低 | 第5天 白天最高 | 第5天 夜间最低 |
|---|---|---|---|---|---|---|
| 单一 IFS（−6.5 K/km） | 1.26 / 80% | 1.06 / 87% | 1.54 / 72% | 1.13 / 84% | 1.80 / 65% | 1.19 / 81% |
| 3 家等权平均，不订正 | 1.11 / 86% | 0.95 / 90% | 1.40 / 76% | 0.97 / 90% | 1.60 / 70% | 1.09 / 86% |
| 3 家平均 + 统一偏差（多模式方案的订正法） | 0.98 / 89% | 0.92 / 92% | 1.27 / 79% | 0.95 / 91% | **1.40 / 76%** | 1.10 / 85% |
| **自主引擎（加权共识 + 偏差，昼夜不同递减率）** | **0.95 / 90%** | **0.86 / 94%** | **1.22 / 82%** | **0.92 / 92%** | 1.43 / 74% | **1.06 / 86%** |

降水与风（同一窗口，每格约 470 对；晴雨 PC 为 12 小时 ≥0.1 mm 判对比例，TS 为有雨命中评分，风为蒲福级完全一致的比例）：

| 方案 | 晴雨 第1/3/5天 | TS 第1/3/5天 | 雨日频率偏差 第1天 | 风力级别一致 第1/3/5天 |
|---|---|---|---|---|
| 单一 IFS | 77 / 73 / 68% | 57 / 51 / 47 | 1.49 | 57 / 56 / 52% |
| 3 家平均，不订正 | 69 / 65 / 55% | 51 / 49 / 43 | 1.91 | 53 / 53 / 50% |
| **自主引擎** | **85 / 82 / 78%** | **61 / 58 / 54** | **0.88** | **59 / 58 / 57%** |

春季（4—6 月中）好做：晴雨 89 / 88 / 82%、TS 77 / 76 / 68；盛夏对流雨难得多，拉低了整段的降水成绩。

降水概率（明细）：Brier 技巧评分 0.43（相对气候概率；春季 0.59）；预报 0–20% 时实况有雨 10%，20–50% 时 35%，
50–80% 时 57%，80–100% 时 88%。

**能不能推到乡镇**：乡镇不是训练站点，所以又做了留一站检验——用其余 6 站拟合、给第 7 站打分。
气温 MAE 只增加 0.01–0.04 ℃（第1天白天最高 0.95 → 0.98，夜间最低 0.86 → 0.90，第5天 1.43 → 1.44 / 1.06 → 1.09），
仍明显好于单一 IFS，说明合并拟合的权重和偏差是片区共性，能用到没有实况的地点。

结论（研究中还比了 OCF、自由回归 MOS、不同 α 与窗口，数字从略）：

- **昼夜分开处理海拔最关键。** 白天用 −6.5 K/km 最好；夜间改用模式自身的局地递减率，夜间最低 MAE 每个时效再降
  0.05–0.08 ℃（第1天 0.94 → 0.86，误差 ≤2 ℃ 比例 91% → 94%）——标准递减率在有逆温的夜里会把山谷站订错。
- 单家里 AIFS 的白天最高最准（第1天 1.10 ℃，IFS 1.26、GFS 1.48），但 6 小时一步取不到凌晨最低，夜间最差（1.73）。
  拟合出的权重：白天 AIFS 占五到六成、GFS 很少甚至为 0；夜间三家接近等权。
- **偏差随季节漂移**：3 家平均的夜间偏差 3 月 +1.2 ℃、4 月 +0.6、5 月 −0.2。60 天等权拟合到 5 月还背着 3、4 月，
  夜间整体偏冷 0.5 ℃；改成近期加权（半衰期 20 天）后春季夜间偏差 −0.51 → −0.37 ℃、平均 MAE 降 1.5%，
  盛夏没有漂移时也不变差（1.009 → 1.004 ℃）。
- 权重合计为 1 的加权 + 偏差，与 OCF（各家偏差订正 + 1/MSE 权重）打平，都比"等权平均 + 统一偏差"好约 4%；
  自由回归 MOS 夜间更差（往平均收缩，会削弱乡镇间的海拔差），没有采用。唯一略输的是第5天白天最高（1.43 对 1.40）。
- 降水：3 家平均后分位数映射最好，与多模式方案结论相同；按家投票、阈值寻优都不如它。
- 降水概率：加入 GEFS 有雨比例**没有**提高技巧（第1天 0.53 对 0.54）；原始 GEFS 比例严重偏自信，
  盛夏技巧为负（−15%，预报 80–100% 时实况只有 57%），所以时段概率只用 3 家确定性模式，不依赖 GEFS 是否取到。
- 风：这片国家站的风比模式**大**约 20%（白天实况均值 3.25 m/s，模式 2.54），乘比例后风力级别一致率 53% → 59%。

与多模式方案的成绩**不能直接对比**：季节不同（这里是春夏，多模式方案的表是 2026 年夏末初秋），实况来源也不同
（这里是 ISD，生产上是 OGIMET）。在生产机上用同一批站、同 30 天比较即可（见下）。

### 部署与切换

```bash
# 1. 一次性：从 AWS 历史数据补齐 60 天存档，再补实况、拟合、回测（网络为主，约 1.5–3 小时）
python3 -m wxgrid backfill --townships yanshan_townships.csv --days 60 --data-dir /var/lib/wxgrid

# 2. 同一批站、同 30 天、同一套评分，对比两套引擎的样本外成绩
python3 -m wxgrid verify --engine native --force
python3 -m wxgrid verify --force

# 3. 切换：wxgrid-publish.service 的 ExecStart 里加 --engine native，然后
systemctl daemon-reload
```

- 一次发布实测（3 家齐全）：墙钟约 3 分钟（含补 OGIMET 实况），CPU 约 35 秒，峰值约 400 MB；按索引读约 0.8 GB GRIB 字节范围
  （IFS 330、GFS 300、AIFS 110、GEFS 50 MB，只在内存里解码裁剪，不落盘）。
  backfill 每个时次约 1–1.5 分钟（受 AWS 限速）。
- 切换后每次发布都把当次 4 家数据存进 `archive/`，训练集自动滚动更新，不用再跑 backfill。
- ECMWF 的 AWS 镜像持续高频访问会回 503 SlowDown（实测连续半小时 20 次/秒触发），所以限速 8 次/秒，失败时改读 data.ecmwf.int。

### 局限

- 只有 3 家确定性模式；回测只覆盖 2025 年 3—8 月（春季、梅雨、盛夏），**秋冬（霜冻、雨雪）尚未检验**。
- 回测实况一部分是 ISD 的 12 h 极值（已与 3 小时实况比对筛过），口径与生产上 OGIMET 的 24 h 极值略有差别；
  降水只有 3 个站有报文原文，样本较少。
- 存档不足（新部署且没跑 backfill）时只做等权平均、不订正，页面检验一节显示"还没有足够的检验样本"。
- 实况仍只有 OGIMET 一个来源；NOAA 的 GHCNh 缺最近的 3 个站且没有 24 h 极值，没有接为备份。

## CLI

| 命令 | 用途 |
|---|---|
| `runs` | 查各源最新已发布 cycle |
| `townships` | 由 Wikidata 或 GeoJSON 生成乡镇表（含 Copernicus DEM 海拔） |
| `daily` | 打印 N 天乡镇预报表 |
| `bulletin` | 县城 + 分乡镇白天/夜间公报（不含概率）；`--run YYYYMMDDHH` 钉某个 cycle，`--out` 另存产品 JSON |
| `fetch` | 下载/降尺度/融合，可存 SQLite 或 NetCDF |
| `calibrate` | 用站点观测拟合逐乡镇订正 |
| `publish` | 算一份产品存入 `--data-dir`；`--engine multimodel\|native\|grib`，`--loop` 可自带调度 |
| `verify` | 更新站点实况与模式存档、重新拟合订正、打印样本外成绩；`--engine native` 看自主引擎 |
| `backfill` | 从 ECMWF / NOAA 在 AWS 上的历史数据补齐自主引擎的存档（默认 60 天），然后拟合 |
| `serve` | 起只读 API + Web 页面 |

## 测试

```bash
cd /opt/wxgrid && python3 -m pytest tests -q
```

全离线（合成数据 + monkeypatch，GRIB 用 ecCodes 自带样板现场生成），不联网。GitHub Actions（`.github/workflows/ci.yml`）
在 Python 3.11 / 3.12 / 3.13 上跑全部测试，并用 ruff 查语法错误和 pyflakes 问题（`ruff.toml`）。

## 数据与口径

- `tp` 统一为“自起报累积、毫米”。GFS 的 `WEASD` 是地面积雪量，`gfs.fetch` 会减去 f000
  基线再交给降尺度，否则起报时已有的积雪会被当成新降雪。
- 气温按 `T_town = T_grid + γ(z_town − z_grid)`，γ 默认 −6.5 K/km，订正上限 ±10 K。
  风和降水默认不做地形假设（系数 1.0），要分离靠 `calibrate` 拟合。
- 融合先各自降尺度到点、再加权，不在原始网格上混，以免抹掉海拔信号。
- 白天 / 夜间：每个 3 小时窗口按其**起始**时刻归入 08—20 或 20—08，每个时段恰好 4 个窗口，缺一个就不出这个时段。
  白天气温取窗口最高气温（ECMWF `mx2t3`、GFS `TMAX`）的最大值，夜间取窗口最低气温的最小值；
  风力范围取 4 个时刻 10 m 风的最小～最大蒲福级，风向为 u/v 矢量平均。
- 用语：时段降水按 **12 小时**国标等级（小雨 0.1–4.9、中雨 5–14.9、大雨 15–29.9、暴雨 30–69.9 mm），
  降雪同理；天空只分晴（0–3 成）/多云（4–7 成）/阴（8–10 成），不用"少云"；风力 ≤2 级写 `<3级`，
  阵风 ≥6 级且高出平均风 2 级以上才注明。
- 逐3小时：原生的 3 小时融合值。气温、风是窗口结束时刻的值，天气与降水是前 3 小时；
  3 小时降水用语按平均雨强（同逐时，见下）。
- 降水时段：列出时段内有 ≥0.1 mm 的 3 小时窗口，连续的合并，写本地钟点（夜间可跨零点，如 `23—02时`）。
- **逐时（参考，页面不展示）**：多模式方案下是 8 家逐小时值的平均，做与 3 小时相同的气温订正，
  降水按所在时段的映射系数缩放。以下为 GRIB 退回方案的做法：IFS 只有 3 小时一步，GFS 逐时到 120 小时。逐时值 = 3 小时融合值的直线插值
  + GFS 在该小时相对它自己直线插值的偏离（`X(h) = B̄(h) + [G(h) − Ḡ(h)]`），节点上与逐日产品完全一致；
  降水把每个 3 小时融合量按 GFS 逐时降水的占比分到各小时（GFS 该时段无雨则均分），3 小时总量守恒。
  逐时 GFS 只取 6 个要素（气温、u/v、累积降水、阵风、总云量），每个时次约 3 MB。
- 逐时降水用语没有国标，按 AMS 雨强：小雨 ≤2.5、中雨 2.6–7.6、大雨 >7.6 mm/h，
  ≥20 mm/h 按气象部门短时强降水标准记“强降水”。
- 阵风：ECMWF 开放数据 90 小时内是 1 小时最大阵风 `10fg`，之后是 3 小时最大阵风 `10fg3`，两者都当 `gust` 用。

## 已知限制

- 降水概率（明细）用的 GEFS cycle 可能比确定性 cycle 新或旧，记在 `meta.pop_run`；
  GEFS 覆盖不到的时段概率为 null。
- 订正用的国家站都在县外（最近的武夷山约 50 km）；县城旁的上饶站不参与国际交换。订正的是片区共同偏差，
  乡镇小气候仍体现不出来。检验窗口只有 30 天，换季时滚动订正要一两周才跟上。
- 多模式数据依赖 Open-Meteo 这一第三方服务（非商业免费）；不想依赖它就用自主引擎 `--engine native`。
- `--tz` 只支持整小时偏移（内部按整小时取整）。
- 逐时形态只来自 GFS 一家；阵性降水的具体钟点不确定性大，按 3–6 小时尺度理解更稳妥。
  逐时只到起报后 120 小时（GFS 逐时上限）。
- 逐时降水概率是所在 6 小时 GEFS 时段的概率，不是该小时本身的概率。
- 多模式与 GRIB 方案的融合风速由加权后的 u/v 求模，成员风向分歧大时会略低于成员风速的加权平均
  （自主引擎改为各家风速平均再按实况比例订正）。
- API 无鉴权且绑 `0.0.0.0:8790`，公网可读全部预报数据（只读，无法篡改）。
- 内置 HTTP 服务是 Python `http.server`，够用但不抗高并发，正式对外建议走反代。
