"""One structured forecast product, shared by the CLI, the scheduler and the API.

``build`` turns the daily / half-day / window aggregates into a plain-JSON dict
(the thing the web UI and the read-only API serve) and a set of deterministic
Chinese conclusions. ``compute`` is the network wrapper: run the pipeline, then
build. Keeping the two apart lets the aggregation be tested offline.

The JSON shape is deliberately flat and self-describing::

    meta        : county, seat, run, init_time, member, sources, weights, tz, days, generated
    days[]      : date, weekday, hours, county summary, per-township cells
    townships[] : id, name, lat, lon, elevation           (static roster)
    conclusions : headline + typed alerts (高温/暴雨/大风/低温/...)
    text        : the plain-text bulletin, verbatim
"""
from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import xarray as xr

from . import bulletin as bulletin_mod
from . import daily as daily_mod
from . import downscale, phenomena, pipeline, probability
from .points import Township
from .sources import gefs as gefs_mod

WEEKDAY = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _f(x: Any) -> float | None:
    """A JSON-safe float: NaN/inf -> None."""
    v = float(x)
    return v if np.isfinite(v) else None


def _weekday(day: np.datetime64) -> str:
    return WEEKDAY[day.astype("datetime64[D]").astype(dt.date).weekday()]


def _cell(daily: xr.Dataset, halves: xr.Dataset, i: int, k: int,
          pop_val: float | None, spans) -> dict[str, Any]:
    text = phenomena.join_halves(
        phenomena.half_day_text(float(halves["day_precip"].values[i, k]),
                                float(halves["day_snow"].values[i, k]),
                                float(halves["day_cloud"].values[i, k])),
        phenomena.half_day_text(float(halves["night_precip"].values[i, k]),
                                float(halves["night_snow"].values[i, k]),
                                float(halves["night_cloud"].values[i, k])),
    )
    wdir = float(daily["wind_dir"].values[i, k])
    if np.isfinite(wdir):
        wind = phenomena.wind_text(wdir, float(daily["wind_speed_min"].values[i, k]),
                                   float(daily["wind_speed_max"].values[i, k]),
                                   float(daily["wind_gust"].values[i, k]))
        wname = daily_mod.compass(wdir)
    else:
        wind, wname = "—", "—"
    return {
        "point": str(daily["point"].values[i]),
        "weather": text,
        "tmin": _f(daily["tmin"].values[i, k]),
        "tmax": _f(daily["tmax"].values[i, k]),
        "tavg": _f(daily["tavg"].values[i, k]),
        "precip": _f(daily["precip"].values[i, k]),
        "snow": _f(daily["snow"].values[i, k]),
        "cloud": _f(daily["cloud"].values[i, k]),
        "pop": None if pop_val is None else _f(pop_val),
        "wind_text": wind,
        "wind_dir": _f(daily["wind_dir"].values[i, k]),
        "wind_dir_name": wname,
        "wind_speed": _f(daily["wind_speed"].values[i, k]),
        "wind_speed_max": _f(daily["wind_speed_max"].values[i, k]),
        "wind_gust": _f(daily["wind_gust"].values[i, k]),
        "windows": [list(s) for s in (spans or [])],
        "windows_text": daily_mod.format_windows(spans),
    }


def build(daily: xr.Dataset, halves: xr.Dataset, windows: dict, *,
          county: str, seat: str, run: str, member: str, sources: tuple[str, ...],
          weights: dict[str, float] | None, tz: float, days: int,
          pop: xr.DataArray | None = None) -> dict[str, Any]:
    """Assemble the JSON product from already-computed aggregates."""
    names = [str(n) for n in daily["name"].values]
    ids = [str(p) for p in daily["point"].values]
    lat = daily["latitude"].values
    lon = daily["longitude"].values
    z = daily["elevation"].values
    zmodel = daily["model_elevation"].values if "model_elevation" in daily else np.full(len(ids), np.nan)
    pop_ids = [str(p) for p in pop["point"].values] if pop is not None else []
    summary = daily_mod.county_summary(daily)

    n_days = min(days, daily.sizes["day"])
    day_out: list[dict[str, Any]] = []
    for k in range(n_days):
        day = daily["day"].values[k]
        label = str(np.datetime64(day, "D"))
        cells = []
        for i in range(len(ids)):
            pv = None
            if pop is not None and ids[i] in pop_ids:
                pv = float(pop.values[pop_ids.index(ids[i]), k])
            spans = (windows or {}).get((ids[i], label))
            cells.append(_cell(daily, halves, i, k, pv, spans))
        day_out.append({
            "date": label,
            "weekday": _weekday(day),
            "hours": int(daily["hours"].values[k]),
            "county": {
                "tmax_max": _f(summary["tmax_max"].values[k]),
                "tmax_mean": _f(summary["tmax_mean"].values[k]),
                "tmin_min": _f(summary["tmin_min"].values[k]),
                "tmin_mean": _f(summary["tmin_mean"].values[k]),
                "precip_max": _f(summary["precip_max"].values[k]),
                "precip_mean": _f(summary["precip_mean"].values[k]),
                "wind_mean": _f(summary["wind_mean"].values[k]),
                "wind_max": _f(summary["wind_max"].values[k]),
                "weather": _county_weather(cells),
                "pop_max": max((c["pop"] for c in cells if c["pop"] is not None), default=None),
            },
            "cells": cells,
        })

    roster = [{"id": ids[i], "name": names[i], "lat": _f(lat[i]), "lon": _f(lon[i]),
               "elevation": _f(z[i]), "model_elevation": _f(zmodel[i])}
              for i in range(len(ids))]

    init_time = str(daily.attrs.get("init_time", ""))
    text = to_bulletin_text(daily, halves, windows, county=county, seat=seat, run=run,
                            member=member, days=n_days, tz=tz, pop=pop, init_time=init_time)
    return {
        "meta": {
            "county": county, "seat": seat, "run": run, "init_time": init_time,
            "member": member, "sources": list(sources),
            "weights": weights, "tz": tz, "days": n_days,
            "n_townships": len(ids),
            "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "source_label": str(daily.attrs.get("source", member)),
            "pop_members": int(pop.attrs.get("members", 0)) if pop is not None else 0,
        },
        "days": day_out,
        "townships": roster,
        "conclusions": conclusions(day_out, county=county, seat=seat, seat_id=_seat_id(names, ids, seat)),
        "text": text,
    }


def _seat_id(names: list[str], ids: list[str], seat: str) -> str | None:
    return ids[names.index(seat)] if seat in names else None


def _county_weather(cells: list[dict]) -> str:
    """The county-wide phrase: the most common township weather that day."""
    from collections import Counter
    if not cells:
        return ""
    return Counter(c["weather"] for c in cells).most_common(1)[0][0]


#: CMA-aligned alert cuts. Wind is Beaufort force on the daily max gust.
def conclusions(days: list[dict], *, county: str, seat: str, seat_id: str | None) -> dict[str, Any]:
    """Deterministic county conclusions: a headline plus typed alerts."""
    alerts: list[dict[str, Any]] = []
    if not days:
        return {"headline": "无有效预报", "alerts": alerts, "seat_id": seat_id}

    tmaxes = [d["county"]["tmax_max"] for d in days if d["county"]["tmax_max"] is not None]
    tmins = [d["county"]["tmin_min"] for d in days if d["county"]["tmin_min"] is not None]
    hi = max(tmaxes) if tmaxes else None
    lo = min(tmins) if tmins else None

    # 高温
    for d in days:
        t = d["county"]["tmax_max"]
        if t is not None and t >= 35.0:
            level = "红色" if t >= 40 else "橙色" if t >= 37 else "黄色"
            alerts.append({"type": "高温", "level": level, "date": d["date"],
                           "detail": f"{d['date']} 全县最高气温达 {t:.0f}℃"})
    # 降水 (24h grade on the county max)
    for d in days:
        p = d["county"]["precip_max"]
        if p is not None and p >= 50.0:
            level = "红色" if p >= 250 else "橙色" if p >= 100 else "黄色"
            alerts.append({"type": "暴雨", "level": level, "date": d["date"],
                           "detail": f"{d['date']} 局地 24 小时降水 {p:.0f} mm（{phenomena.precip_text(p)}）"})
    # 大风 (Beaufort >= 6 on the daily gust)
    for d in days:
        g = d["county"]["wind_max"]
        if g is not None and phenomena.beaufort(g) >= 6:
            f = phenomena.beaufort(g)
            level = "红色" if f >= 11 else "橙色" if f >= 9 else "黄色"
            alerts.append({"type": "大风", "level": level, "date": d["date"],
                           "detail": f"{d['date']} 阵风可达 {f} 级（{g:.0f} m/s）"})
    # 低温 / 霜冻
    for d in days:
        t = d["county"]["tmin_min"]
        if t is not None and t <= 0.0:
            alerts.append({"type": "低温", "level": "蓝色", "date": d["date"],
                           "detail": f"{d['date']} 全县最低气温 {t:.0f}℃，注意防霜冻"})

    rainy = [d["date"] for d in days if (d["county"]["pop_max"] or 0) >= 60
             or (d["county"]["precip_mean"] or 0) >= 0.1]
    parts = [f"未来{len(days)}天{county}"]
    if hi is not None and lo is not None:
        parts.append(f"气温 {lo:.0f}～{hi:.0f}℃")
    if rainy:
        parts.append("有降水日：" + "、".join(x[5:] for x in rainy))
    else:
        parts.append("以多云到晴为主")
    if alerts:
        kinds = sorted({a["type"] for a in alerts})
        parts.append("注意" + "、".join(kinds))
    # seat_id lets clients pick the county seat's cell without matching on names.
    return {"headline": "，".join(parts) + "。", "alerts": alerts, "seat_id": seat_id}


def to_bulletin_text(daily, halves, windows, *, county, seat, run, member, days, tz,
                     pop, init_time) -> str:
    """Render the same plain-text bulletin the CLI prints."""
    blocks = [
        bulletin_mod.header(county, run, np.asarray(init_time, dtype="datetime64[m]") if init_time else
                            np.datetime64("NaT"), daily.sizes["point"], days, member, tz),
        "",
        bulletin_mod.seat_forecast(daily, halves, seat, pop=pop, windows=windows, days=days, tz=tz),
        "",
        bulletin_mod.day_blocks(daily, halves, pop=pop, windows=windows, days=days),
    ]
    return "\n".join(blocks)


def compute(points: list[Township], *, county: str, seat: str, days: int = 5, every: int = 3,
            sources: tuple[str, ...] = ("ecmwf", "gfs"), member: str = "blend",
            weights: dict[str, float] | None = None, tz: float = daily_mod.TZ_CHINA,
            pad: float = 0.75, want_pop: bool = True, workers: int = 4,
            min_age_hours: float | None = None, run=None, sess=None) -> dict[str, Any]:
    """Run the pipeline for one county and return the JSON product.

    Network-bound. ``run`` pins a cycle (a :class:`wxgrid.runs.Run`); otherwise the
    newest cycle all sources publish deep enough is used.
    """
    sess = sess or pipeline.session()
    steps = pipeline.step_grid(days * 24, every)
    if run is None:
        run = pipeline.common_run(sources, steps, sess=sess, min_age_hours=min_age_hours)
    member_ds = pipeline.forecast(points, steps=steps, sources=sources, pad=pad,
                                  weights=weights, cfg=downscale.DownscaleConfig(),
                                  sess=sess, run=run)
    if member not in member_ds:
        raise KeyError(f"member {member!r} not produced; have {sorted(member_ds)}")
    chosen = member_ds[member]
    dly = daily_mod.to_daily(chosen, tz_hours=tz)
    halves = daily_mod.half_day(chosen, tz_hours=tz)
    windows = daily_mod.precip_windows(chosen, tz_hours=tz)

    pop = None
    if want_pop:
        ens_steps = list(range(6, days * 24 + 1, 6))
        kw = {"min_age_hours": min_age_hours} if min_age_hours else {}
        ens_run = next((c for c in gefs_mod.candidate_runs(**kw)
                        if gefs_mod.probe_run(sess, c, max(ens_steps))), None)
        if ens_run is not None:
            ens = gefs_mod.fetch_ensemble(points, ens_run, ens_steps, sess=sess, max_workers=workers)
            pop = probability.daily_pop(ens, dly["day"].values, tz)

    return build(dly, halves, windows, county=county, seat=seat, run=str(run),
                 member=member, sources=sources, weights=weights, tz=tz, days=days, pop=pop)
