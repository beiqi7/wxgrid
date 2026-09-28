# wxgrid

乡镇级精细化天气预报。ECMWF IFS + NOAA GFS（0.25°）按海拔递减率降尺度到乡镇点，加权融合，
按北京时分**白天（08—20 时）/ 夜间（20—次日 08 时）**写五天预报，另给 0—72 小时逐3小时预报；
GEFS 21 成员的降水概率只放在明细里，不进预报用语。产出文字预报、结构化 JSON、只读 API 和 Web 页面。

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

整机 4 核 / 8 GB，目标：wxgrid 全部合计 **CPU ≤ 30%、内存 ≤ 50%**。两个服务都放进
`wxgrid.slice`，由 slice 统一封顶，单元各自再有更紧的上限：

| 单元 | CPUQuota | MemoryMax | Nice | 实测 |
|---|---|---|---|---|
| `wxgrid.slice`（合计） | 120%（=1.2 核 = 30%） | 4 GB（50%） | — | 上限，两者叠加也不会超过 |
| publish（每天 2 次） | 120% | 2 GB | 15 | 见下 |
| web（常驻） | 25% | 384 MB | 10 | 空闲接近 0，约 30 MB |

publish 实测（含逐时，5 天）：墙钟约 9–11 分钟，CPU 约 2 分 15 秒（平均 0.25 核，约 6%），
峰值常驻内存 约 580 MB（整机 7%），任意 5 秒窗口内 slice 最高 0.77 核（19%）。cgroup 内存读数会更高，因为它把写 GRIB 缓存产生的页缓存也算进去，
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

## CLI

| 命令 | 用途 |
|---|---|
| `runs` | 查各源最新已发布 cycle |
| `townships` | 由 Wikidata 或 GeoJSON 生成乡镇表（含 Copernicus DEM 海拔） |
| `daily` | 打印 N 天乡镇预报表 |
| `bulletin` | 县城 + 分乡镇白天/夜间公报（不含概率）；`--run YYYYMMDDHH` 钉某个 cycle，`--out` 另存产品 JSON |
| `fetch` | 下载/降尺度/融合，可存 SQLite 或 NetCDF |
| `calibrate` | 用站点观测拟合逐乡镇订正 |
| `publish` | 算一份产品存入 `--data-dir`；`--loop` 可自带调度 |
| `serve` | 起只读 API + Web 页面 |

## 测试

```bash
cd /opt/wxgrid && python3 -m pytest tests -q
```

全离线（合成数据 + monkeypatch），不联网。

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
- **逐时（参考，页面不展示）**：IFS 只有 3 小时一步，GFS 逐时到 120 小时。逐时值 = 3 小时融合值的直线插值
  + GFS 在该小时相对它自己直线插值的偏离（`X(h) = B̄(h) + [G(h) − Ḡ(h)]`），节点上与逐日产品完全一致；
  降水把每个 3 小时融合量按 GFS 逐时降水的占比分到各小时（GFS 该时段无雨则均分），3 小时总量守恒。
  逐时 GFS 只取 6 个要素（气温、u/v、累积降水、阵风、总云量），每个时次约 3 MB。
- 逐时降水用语没有国标，按 AMS 雨强：小雨 ≤2.5、中雨 2.6–7.6、大雨 >7.6 mm/h，
  ≥20 mm/h 按气象部门短时强降水标准记“强降水”。
- 阵风：ECMWF 开放数据 90 小时内是 1 小时最大阵风 `10fg`，之后是 3 小时最大阵风 `10fg3`，两者都当 `gust` 用。

## 已知限制

- 降水概率（明细）用的 GEFS cycle 可能比确定性 cycle 新或旧，记在 `meta.pop_run`；
  GEFS 覆盖不到的时段概率为 null。
- 全球模式普遍有"毛毛雨偏差"：零点几毫米的小雨常常实际没下。
- `--tz` 只支持整小时偏移（内部按整小时取整）。
- 逐时形态只来自 GFS 一家；阵性降水的具体钟点不确定性大，按 3–6 小时尺度理解更稳妥。
  逐时只到起报后 120 小时（GFS 逐时上限）。
- 逐时降水概率是所在 6 小时 GEFS 时段的概率，不是该小时本身的概率。
- 融合风速由加权后的 u/v 求模，成员风向分歧大时会略低于成员风速的加权平均。
- API 无鉴权且绑 `0.0.0.0:8790`，公网可读全部预报数据（只读，无法篡改）。
- 内置 HTTP 服务是 Python `http.server`，够用但不抗高并发，正式对外建议走反代。
