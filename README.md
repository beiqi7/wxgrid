# wxgrid

乡镇级精细化天气预报。ECMWF IFS + NOAA GFS（0.25°）按海拔递减率降尺度到乡镇点，加权融合，
按北京时切日/昼夜，GEFS 21 成员出降水概率，产出文字预报、结构化 JSON、只读 API 和 Web 页面。

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
install -m644 deploy/*.service deploy/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now wxgrid-web.service wxgrid-publish.timer
```

- `wxgrid-publish.timer` 每天北京时 **07:30 / 19:30** 各跑一次（各带 ≤10 分钟随机延迟）。
  这两个点分别在 ECMWF 00Z、12Z 开放数据落地之后。同一 cycle 已在盘上会直接跳过，不重复下载。
- `wxgrid-web.service` 常驻，只读 `/var/lib/wxgrid`，不做任何计算。
- 换县：改 `wxgrid-publish.service` 里的 `--townships/--county/--seat`。

### 资源限额

整机 4 核 / 8 GB，目标占用约 30%：

| 单元 | CPUQuota | MemoryMax | Nice | 实测 |
|---|---|---|---|---|
| publish（每天 2 次） | 120%（=1.2 核） | 2 GB | 15 | 约 10 分钟墙钟 / 2 分钟 CPU，峰值内存约 670 MB |
| web（常驻） | 25% | 384 MB | 10 | 空闲接近 0 |

两个单元都开了 `ProtectSystem=strict`；publish 仅可写 `/var/lib/wxgrid`，web 对它只读。

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

```bash
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
conclusions  headline, alerts[]{type,level,date,detail}
text         与 CLI 一致的文字预报全文
```

`hours < 24` 表示该日只被部分覆盖（首日常见），页面和文稿都会标注。

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

## 已知限制

- 降水概率用的 GEFS cycle 可能比确定性 cycle 新或旧；两者都会记在 `meta`/页面上。
  若 GEFS 落后，最后一天的概率可能只被部分时段覆盖。
- `--tz` 只支持整小时偏移（内部按整小时取整）。
- 融合风速由加权后的 u/v 求模，成员风向分歧大时会略低于成员风速的加权平均。
- API 无鉴权且绑 `0.0.0.0:8790`，公网可读全部预报数据（只读，无法篡改）。
- 内置 HTTP 服务是 Python `http.server`，够用但不抗高并发，正式对外建议走反代。
