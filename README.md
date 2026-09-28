# wxgrid

乡镇级精细化天气预报。ECMWF IFS + NOAA GFS（0.25°）按海拔递减率降尺度到乡镇点，加权融合，
按北京时切日/昼夜，GEFS 21 成员出降水概率，产出逐日与逐时（至 120 小时）预报、文字预报、
结构化 JSON、只读 API 和 Web 页面。

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
  07:30 取前一天 12Z（UTC）起报，19:30 取当天 00Z 起报；这两个时次那时都已在开放数据上落地。
  publish 先解析出要用的时次，产品和逐时文件都已在盘上就直接返回，不下载任何东西；
  旧产品缺逐时文件时会补算一次。
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

1. 县城此刻（当前小时的逐时预报）与今日最高/最低 → 预警
2. **逐时预报**：可选任一乡镇，横向滚动的逐时图（气温曲线、降水柱、6 小时降水概率、风向风力、夜间底色），
   按天跳转，下方可展开逐时明细表
3. 未来五天（点某天切换下方乡镇列表）
4. 各乡镇按海拔从高到低、气温条共用一条色标；点一行打开详情：逐时图 + 逐时表 + 五天表
5. **模型与方法**：数据源、六步流程、三个公式、用语阈值、局限
6. 折叠的文稿与接口说明

日/夜主题存在 `localStorage`。页面只读 `/api/latest`、`/api/runs`、`/api/hourly/<乡镇>`，可在顶栏切换历史起报时次。

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
| GET | `/api/hourly/<id 或名称>` | 某乡镇逐时预报，按行；`?hours=N` 取前 N 小时，`?run=<file>` 取历史时次 |
| GET | `/api/hourly` | 全部乡镇逐时预报，按列（约 100 KB） |

```bash
# 河口镇未来 24 小时（名称要 URL 编码；也可以用 id，如 Q31855302）
curl -s 'http://localhost:8790/api/hourly/%E6%B2%B3%E5%8F%A3%E9%95%87?hours=24' | python3 -m json.tool
curl -s http://localhost:8790/api/summary | python3 -m json.tool
curl -s http://localhost:8790/api/latest | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["conclusions"]["headline"])'
```

### 产品 JSON 结构

```
meta         county/seat/run/init_time/member/sources/weights/tz/days/n_townships/generated/pop_members
days[]       date, weekday, hours(覆盖小时), county{tmax_max,tmin_min,...,weather,pop_max}
  cells[]    point, weather, tmin/tmax/tavg, precip, snow, cloud, pop,
             wind_text/wind_dir/wind_dir_name/wind_speed/wind_speed_max/wind_gust,
             windows(本地小时区间), windows_text
townships[]  id, name, lat, lon, elevation, model_elevation
conclusions  headline, alerts[]{type,level,date,detail}, seat_id(县城乡镇 id，找不到时为 null)
text         与 CLI 一致的文字预报全文
```

`hours < 24` 表示该日只被部分覆盖（首日常见），页面和文稿都会标注。

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

结论里的 alert 阈值按国标：高温 ≥35/37/40℃ 对黄/橙/红，降水 24h ≥50/100/250 mm 对
暴雨/大暴雨/特大暴雨，大风取日最大阵风 ≥6 级，低温 ≤0℃。

## CLI

| 命令 | 用途 |
|---|---|
| `runs` | 查各源最新已发布 cycle |
| `townships` | 由 Wikidata 或 GeoJSON 生成乡镇表（含 Copernicus DEM 海拔） |
| `daily` | 打印 N 天乡镇预报表 |
| `bulletin` | 县城 + 分乡镇公报，含降水概率；`--run YYYYMMDDHH` 可钉某个 cycle |
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
- 昼夜切分按时段**起始**小时落在 08–20 / 20–08，不是按时段结束时刻。
- 跨本地午夜的降水时段会在午夜切开，保证每一天都列出自己的时段。
- **逐时**：IFS 只有 3 小时一步，GFS 逐时到 120 小时。逐时值 = 3 小时融合值的直线插值
  + GFS 在该小时相对它自己直线插值的偏离（`X(h) = B̄(h) + [G(h) − Ḡ(h)]`），节点上与逐日产品完全一致；
  降水把每个 3 小时融合量按 GFS 逐时降水的占比分到各小时（GFS 该时段无雨则均分），3 小时总量守恒。
  逐时 GFS 只取 6 个要素（气温、u/v、累积降水、阵风、总云量），每个时次约 3 MB。
- 逐时降水用语没有国标，按 AMS 雨强：小雨 ≤2.5、中雨 2.6–7.6、大雨 >7.6 mm/h，
  ≥20 mm/h 按气象部门短时强降水标准记“强降水”。
- 阵风：ECMWF 开放数据 90 小时内是 1 小时最大阵风 `10fg`，之后是 3 小时最大阵风 `10fg3`，两者都当 `gust` 用。

## 已知限制

- 降水概率用的 GEFS cycle 可能比确定性 cycle 新或旧；两者都会记在 `meta`/页面上。
  若 GEFS 落后，最后一天的概率可能只被部分时段覆盖。
- `--tz` 只支持整小时偏移（内部按整小时取整）。
- 逐时形态只来自 GFS 一家；阵性降水的具体钟点不确定性大，按 3–6 小时尺度理解更稳妥。
  逐时只到起报后 120 小时（GFS 逐时上限）。
- 逐时降水概率是所在 6 小时 GEFS 时段的概率，不是该小时本身的概率。
- 融合风速由加权后的 u/v 求模，成员风向分歧大时会略低于成员风速的加权平均。
- API 无鉴权且绑 `0.0.0.0:8790`，公网可读全部预报数据（只读，无法篡改）。
- 内置 HTTP 服务是 Python `http.server`，够用但不抗高并发，正式对外建议走反代。
