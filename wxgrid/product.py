"""One structured forecast product, shared by the CLI, the scheduler and the API.

The product is written the way a Chinese public forecast is: in 白天 / 夜间
periods (see :mod:`wxgrid.periods`), each with its weather, its temperature
(白天最高 / 夜间最低) and its wind — and *no* probability in the headline
forecast. Probabilities stay in the detail fields (``cells[].pop``,
``series3h.points[].pop``) for whoever wants them.

``build`` is pure (testable offline); ``compute_bundle`` is the network wrapper.

JSON shape::

    meta          county, seat, run, init_time, issue_time/issue_local, member,
                  sources, weights, source_label, tz, n_periods, n_townships,
                  pop_members, pop_run, generated, series_hours
    periods[]     kind(day|night), name(白天|夜间), date, weekday, rel, label,
                  start_local/end_local/start_lead/end_lead, county{...}, cells[]
      cells[]     point, weather, temp (最高 for 白天 / 最低 for 夜间), tmax, tmin, tmean,
                  precip, precip_3h_max, snow, cloud, wind_text, wind_name, wind_dir,
                  force_lo, force_hi, wind_speed, wind_speed_max, gust, gust_force,
                  windows, windows_text, pop (detail only)
    days[]        date, weekday, rel, day (period index | null), night (index | null)
    series3h      times[] (local window end), lead_h[], points{id: {weather[], temp[],
                  precip[], snow[], cloud[], wind_dir[], wind_name[], wind_force[],
                  wind_speed[], gust[], pop[]}}
    townships[]   id, name, lat, lon, elevation, model_elevation
    conclusions   headline, alerts[]{type, level, date, detail, criterion}, seat_id
    text          the plain-text bulletin, verbatim (no probabilities)
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Any

import numpy as np
import xarray as xr
from numpy.lib.stride_tricks import sliding_window_view

from . import bulletin as bulletin_mod
from . import daily as daily_mod
from . import downscale, phenomena, pipeline, probability
from . import hourly as hourly_mod
from . import periods as periods_mod
from .points import Township
from .sources import REGISTRY
from .sources import gefs as gefs_mod
from .sources import gfs as gfs_mod_det

WEEKDAY = periods_mod.WEEKDAY
#: Length of the public 3-hourly series. 0–72 h at 3 h is the CMA 城镇精细化
#: format and the range where the 3-hourly blend is at its best.
SERIES_HOURS = 72
#: Product JSON shape version (see wxgrid.publish._is_current).
SCHEMA = 2


def _f(x: Any, nd: int | None = None) -> float | None:
    """A JSON-safe float: NaN/inf -> None."""
    v = float(x)
    if not np.isfinite(v):
        return None
    return round(v, nd) if nd is not None else v


def _weekday(date: str) -> str:
    return WEEKDAY[np.datetime64(date, "D").astype(dt.date).weekday()]


def _utc_h(ts) -> int:
    return int(np.datetime64(ts, "h").astype("int64"))


def _span_text(spans: list[tuple[int, int]]) -> str:
    if not spans:
        return "无"
    parts = []
    for a, b in spans:
        e = b % 24 or 24
        parts.append(f"{a % 24:02d}—{e:02d}时")
    return "、".join(parts)


def _windows(precip_row: np.ndarray, steps: np.ndarray, p: periods_mod.Period, init_local_h: int,
             gap: int, threshold: float = 0.1) -> list[tuple[int, int]]:
    """Wet 3-hour windows inside a period, merged, as local clock hours."""
    spans: list[list[int]] = []
    for j, s in enumerate(steps):
        if s - gap < p.start_lead or s > p.end_lead or not precip_row[j] >= threshold:
            continue
        a, b = init_local_h + int(s) - gap, init_local_h + int(s)
        if spans and spans[-1][1] == a:
            spans[-1][1] = b
        else:
            spans.append([a, b])
    return [(a, b) for a, b in spans]


def _cell(agg: xr.Dataset, i: int, k: int, p: periods_mod.Period, pop_val, spans) -> dict[str, Any]:
    g = lambda v: float(agg[v].values[i, k])  # noqa: E731
    wdir = g("wind_dir")
    lo, hi = phenomena.beaufort(g("wind_speed_min")), phenomena.beaufort(g("wind_speed_max"))
    ok = np.isfinite(wdir)
    tmax, tmin = g("tmax"), g("tmin")
    return {
        "point": str(agg["point"].values[i]),
        "weather": phenomena.half_day_text(g("precip"), g("snow"), g("cloud")) if np.isfinite(g("precip")) else "—",
        "temp": _f(tmax if p.kind == "day" else tmin, 2),
        "tmax": _f(tmax, 2), "tmin": _f(tmin, 2), "tmean": _f(g("tmean"), 2),
        "precip": _f(g("precip"), 2), "precip_3h_max": _f(g("precip_3h_max"), 2),
        "snow": _f(g("snow"), 2), "cloud": _f(g("cloud"), 3),
        "wind_text": phenomena.wind_text(wdir, g("wind_speed_min"), g("wind_speed_max"), g("gust")) if ok else "—",
        "wind_name": phenomena.wind_name(wdir) if ok else None,
        "wind_dir": _f(wdir, 0),
        "force_lo": lo if ok else None, "force_hi": hi if ok else None,
        "force_text": phenomena.force_text(lo, hi) if ok else None,
        "wind_speed": _f(g("wind_speed"), 2), "wind_speed_max": _f(g("wind_speed_max"), 2),
        "gust": _f(g("gust"), 2),
        "gust_force": phenomena.beaufort(g("gust")) if np.isfinite(g("gust")) else None,
        "windows": [list(s) for s in spans], "windows_text": _span_text(spans),
        "pop": None if pop_val is None or not np.isfinite(pop_val) else round(float(pop_val)),
    }


def _majority(values: list[str]) -> str:
    vals = [v for v in values if v and v != "—"]
    return Counter(vals).most_common(1)[0][0] if vals else "—"


def _series3h(ds: xr.Dataset, *, issue_utc_h: float, tz: float, hours: int,
              ens=None) -> dict[str, Any]:
    """The public 3-hourly series: window ends after the issue time, for ``hours``."""
    steps = ds["step"].values.astype(int)
    gap = int(np.median(np.diff(steps))) if len(steps) > 1 else 3
    init_h = _utc_h(ds.attrs["init_time"])
    issue_lead = issue_utc_h - init_h
    keep = (steps > issue_lead) & (steps <= issue_lead + hours)
    st = steps[keep]
    off = int(round(tz))
    times = [str(np.datetime64(init_h + int(s) + off, "h"))[:13] + ":00" for s in st]
    pop = hourly_mod.hourly_pop(ens, np.datetime64(init_h, "h"), st) if ens is not None else None

    def col(var):
        return ds[var].transpose("point", "step").values[:, keep].astype(float) if var in ds else None

    t, pr, sn, cl = col("t2m"), col("precip"), col("snow"), col("cloud")
    u, v, gu = col("u10"), col("v10"), col("gust")
    spd = np.hypot(u, v)
    wd = (270.0 - np.degrees(np.arctan2(v, u))) % 360.0
    pts: dict[str, Any] = {}
    for i, pid in enumerate(ds["point"].values):
        s_i = sn[i] if sn is not None else np.zeros_like(pr[i])
        c_i = cl[i] if cl is not None else np.zeros_like(pr[i])
        pts[str(pid)] = {
            "weather": [phenomena.window_text(float(a), float(b), float(c), gap) for a, b, c in zip(pr[i], s_i, c_i)],
            "temp": [_f(x, 1) for x in t[i]],
            "precip": [_f(x, 1) for x in pr[i]],
            "snow": [_f(x, 1) for x in s_i],
            "cloud": [_f(x * 100, 0) for x in c_i],
            "wind_dir": [_f(x, 0) for x in wd[i]],
            "wind_name": [phenomena.wind_name(float(x)) for x in wd[i]],
            "wind_force": [phenomena.beaufort(float(x)) for x in spd[i]],
            "wind_speed": [_f(x, 1) for x in spd[i]],
            "gust": [_f(x, 1) for x in gu[i]] if gu is not None else None,
            "pop": [None if not np.isfinite(x) else round(float(x)) for x in pop[i]] if pop is not None else None,
        }
    return {"step_hours": gap, "times": times, "lead_h": [int(s) for s in st], "points": pts,
            "time_convention": "times are local window ends; precip/snow cover the 3 hours before, "
                               "temp/wind are the values at that time"}


def build(ds: xr.Dataset, *, periods: list[periods_mod.Period], issue_utc, county: str, seat: str,
          run: str, member: str, sources: tuple[str, ...], weights: dict[str, float] | None,
          tz: float, ens=None, ens_run: str | None = None,
          series_hours: int = SERIES_HOURS) -> dict[str, Any]:
    """Assemble the JSON product from a (point, step) township dataset."""
    if not periods:
        raise ValueError("no forecast periods — the run does not reach the issue time")
    agg = periods_mod.aggregate(ds, periods)
    names = [str(n) for n in ds["name"].values]
    ids = [str(p) for p in ds["point"].values]
    steps = ds["step"].values.astype(int)
    gap = int(np.median(np.diff(steps))) if len(steps) > 1 else 3
    init_h = _utc_h(ds.attrs["init_time"])
    off = int(round(tz))
    precip = ds["precip"].transpose("point", "step").values.astype(float)
    issue_h = float(np.datetime64(issue_utc, "s").astype("int64")) / 3600.0
    issue_date = str(np.datetime64(int((issue_h + off) // 24), "D"))

    pop = None
    if ens is not None:
        s_utc = np.array([init_h + p.start_lead for p in periods])
        pop = probability.window_pop(ens, s_utc, s_utc + periods_mod.PERIOD_H)
        pop_ids = list(ens.point_ids)

    out_periods: list[dict[str, Any]] = []
    for k, p in enumerate(periods):
        cells = []
        for i, pid in enumerate(ids):
            pv = pop[pop_ids.index(pid), k] if pop is not None and pid in pop_ids else None
            cells.append(_cell(agg, i, k, p, pv, _windows(precip[i], steps, p, init_h + off, gap)))
        temps = [c["temp"] for c in cells if c["temp"] is not None]
        pr = [c["precip"] for c in cells if c["precip"] is not None]
        rel = periods_mod.relative_label(p.date, issue_date)
        out_periods.append({
            **p.as_dict(), "index": k, "name": p.name, "weekday": _weekday(p.date),
            "rel": rel, "label": periods_mod.period_label(p.date, p.kind, issue_date),
            "county": {
                "weather": _majority([c["weather"] for c in cells]),
                "temp_min": min(temps) if temps else None, "temp_max": max(temps) if temps else None,
                "precip_max": max(pr) if pr else None,
                "precip_mean": float(np.mean(pr)) if pr else None,
                "force_max": max((c["force_hi"] for c in cells if c["force_hi"] is not None), default=None),
                "gust_force_max": max((c["gust_force"] for c in cells if c["gust_force"] is not None), default=None),
                "pop_max": max((c["pop"] for c in cells if c["pop"] is not None), default=None),
            },
            "cells": cells,
        })

    days: list[dict[str, Any]] = []
    for q in out_periods:
        if not days or days[-1]["date"] != q["date"]:
            days.append({"date": q["date"], "weekday": q["weekday"], "rel": q["rel"], "day": None, "night": None})
        days[-1][q["kind"]] = q["index"]

    zmodel = ds["model_elevation"].values if "model_elevation" in ds.coords else np.full(len(ids), np.nan)
    roster = [{"id": ids[i], "name": names[i], "lat": _f(ds["latitude"].values[i]),
               "lon": _f(ds["longitude"].values[i]), "elevation": _f(ds["elevation"].values[i]),
               "model_elevation": _f(zmodel[i])} for i in range(len(ids))]
    seat_id = ids[names.index(seat)] if seat in names else None

    doc = {
        "meta": {
            "schema": SCHEMA,
            "county": county, "seat": seat, "run": run, "init_time": str(ds.attrs.get("init_time", "")),
            "issue_time": str(np.datetime64(issue_utc, "s")) + "Z",
            "issue_local": str(np.datetime64(int(round(issue_h * 60)) + off * 60, "m")),
            "member": member, "sources": list(sources), "weights": weights,
            "source_label": str(ds.attrs.get("source", member)),
            "tz": tz, "n_periods": len(out_periods), "n_townships": len(ids),
            "series_hours": series_hours,
            "pop_members": len(ens.members) if ens is not None else 0,
            "pop_run": ens_run,
            "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
        "periods": out_periods,
        "days": days,
        "series3h": _series3h(ds, issue_utc_h=issue_h, tz=tz, hours=series_hours, ens=ens),
        "townships": roster,
    }
    doc["conclusions"] = conclusions(doc, ds, seat_id=seat_id, tz=tz)
    doc["text"] = bulletin_mod.render(doc)
    return doc


# ---------------------------------------------------------------- conclusions

#: 暴雨预警信号 criteria: window hours, mm, level. The last row is the plain 暴雨
#: grade (24 h >= 50 mm) below every signal standard, reported as 关注.
RAIN_SIGNALS = ((3, 100.0, "红色"), (3, 50.0, "橙色"), (6, 50.0, "黄色"), (12, 50.0, "蓝色"),
                (24, 50.0, "关注"))
#: 大风预警信号: (mean-wind force, gust force, level), strongest first.
WIND_SIGNALS = ((12, 13, "红色"), (10, 11, "橙色"), (8, 9, "黄色"), (6, 7, "蓝色"))


def conclusions(doc: dict, ds: xr.Dataset, *, seat_id: str | None, tz: float) -> dict[str, Any]:
    """Headline plus typed alerts, each tied to the criterion it meets.

    Levels name the colour of the *signal standard* the forecast reaches; these
    are model-based prompts, not signals issued by a meteorological office.
    ``关注`` marks a hazard below the lowest signal standard.
    """
    periods = doc["periods"]
    alerts: list[dict[str, Any]] = []
    days = [q for q in periods if q["kind"] == "day"]

    # 高温: 红 >=40, 橙 >=37 (24 h), 黄 = three consecutive days >=35
    hot = [(q["date"], q["county"]["temp_max"]) for q in days if q["county"]["temp_max"] is not None]
    run_len = 0
    for j, (date, t) in enumerate(hot):
        run_len = run_len + 1 if t >= 35.0 else 0
        if t >= 40:
            lvl, crit = "红色", "日最高气温≥40℃"
        elif t >= 37:
            lvl, crit = "橙色", "日最高气温≥37℃"
        elif t >= 35 and run_len >= 3:
            lvl, crit = "黄色", "连续三天日最高气温≥35℃"
        elif t >= 35:
            lvl, crit = "关注", "日最高气温≥35℃"
        else:
            continue
        alerts.append({"type": "高温", "level": lvl, "date": date, "criterion": crit,
                       "detail": f"局地最高气温{t:.0f}℃"})

    # 暴雨: running 3/6/12 h sums over the 3-hourly township precip
    steps = ds["step"].values.astype(int)
    gap = int(np.median(np.diff(steps))) if len(steps) > 1 else 3
    init_h = _utc_h(ds.attrs["init_time"])
    off = int(round(tz))
    first_lead = periods[0]["start_lead"]
    use = steps - gap >= first_lead
    pr = ds["precip"].transpose("point", "step").values.astype(float)[:, use]
    ends = steps[use]
    dates = [str(np.datetime64((init_h + int(s) + off - 1) // 24, "D")) for s in ends]
    best: dict[str, tuple[int, str, str, float]] = {}
    rank = {"蓝色": 1, "黄色": 2, "橙色": 3, "红色": 4, "关注": 0}
    for hours, mm, lvl in RAIN_SIGNALS:
        n = max(1, hours // gap)
        if pr.shape[1] < n:
            continue
        win = sliding_window_view(np.nan_to_num(pr), n, axis=1)       # (point, start, n)
        sums, peak = win.sum(axis=2), win.argmax(axis=2)
        # a window counts for the date of its wettest 3 hours, so one downpour is
        # one event even though several 24 h windows (ending on two dates) contain it
        for i, j in zip(*np.nonzero(sums >= mm)):
            date, v = dates[j + peak[i, j]], float(sums[i, j])
            cur = best.get(date)
            if cur is None or rank[lvl] > cur[0] or (rank[lvl] == cur[0] and v > cur[3]):
                best[date] = (rank[lvl], lvl, f"{hours}小时降水量≥{mm:.0f}毫米", v)
    for date, (_, lvl, crit, v) in sorted(best.items()):
        alerts.append({"type": "暴雨", "level": lvl, "date": date, "criterion": crit,
                       "detail": f"局地{crit[:crit.index('小时') + 2]}降水量约{v:.0f}毫米"})
    # 大风: mean-wind force or gust force, per period
    for q in periods:
        fm, gm = q["county"]["force_max"] or 0, q["county"]["gust_force_max"] or 0
        for f_mean, f_gust, lvl in WIND_SIGNALS:
            if fm >= f_mean or gm >= f_gust:
                alerts.append({"type": "大风", "level": lvl, "date": q["date"],
                               "criterion": f"平均风力≥{f_mean}级或阵风≥{f_gust}级",
                               "detail": f"{q['label']}平均风力{fm}级，阵风{gm}级"})
                break

    # 低温 / 霜冻: a night minimum at or below 0 ℃
    for q in periods:
        t = q["county"]["temp_min"] if q["kind"] == "night" else None
        if t is not None and t <= 0.0:
            alerts.append({"type": "霜冻", "level": "关注", "date": q["date"], "criterion": "夜间最低气温≤0℃",
                           "detail": f"{q['label']}局地最低气温{t:.0f}℃"})

    # one alert per (type, date): keep the highest level
    keep: dict[tuple[str, str], dict] = {}
    for a in alerts:
        key = (a["type"], a["date"])
        if key not in keep or rank[a["level"]] > rank[keep[key]["level"]]:
            keep[key] = a
    alerts = sorted(keep.values(), key=lambda a: (a["date"], a["type"]))
    return {"headline": headline(doc, seat_id, alerts), "alerts": alerts, "seat_id": seat_id}


def _rain_word(texts: list[str]) -> str | None:
    rains = [t for t in texts if t in phenomena.RAIN_RANK]
    return max(rains, key=phenomena.RAIN_RANK.get) if rains else None


def _md(date: str) -> str:
    return f"{int(date[5:7])}月{int(date[8:10])}日"


def _date_span(dates: list[str]) -> str:
    """``10月1日`` / ``10月1日—2日`` / ``9月30日—10月2日`` for a run of dates."""
    a, b = dates[0], dates[-1]
    if a == b:
        return _md(a)
    return f"{_md(a)}—{int(b[8:10])}日" if a[5:7] == b[5:7] else f"{_md(a)}—{_md(b)}"


def headline(doc: dict, seat_id: str | None, alerts: list[dict]) -> str:
    """A bulletin-style summary: the next two periods at the seat, the rain, the temperatures."""
    periods = doc["periods"]
    county = doc["meta"]["county"]
    parts: list[str] = []
    seat_cells = [next((c for c in q["cells"] if c["point"] == seat_id), q["cells"][0]) for q in periods]
    if periods:
        near = [f"{q['label']}{c['weather']}" for q, c in zip(periods[:2], seat_cells[:2])]
        parts.append(f"{doc['meta']['seat'] or county}" + "，".join(near))

    # Rain after the two periods already named. Per date: the grade most townships
    # reach (the "有X雨") and the heaviest anywhere (the "局地"); consecutive dates
    # with the same wording are one clause.
    per_date: dict[str, list[list[str]]] = {}
    for q in periods[2:]:
        per_date.setdefault(q["date"], []).append([c["weather"] for c in q["cells"]])
    clauses: list[tuple[list[str], str | None, str | None]] = []
    for date in sorted(per_date):
        halves = per_date[date]
        n_town = len(halves[0])
        # a township's grade for the date is its wetter half
        town = [_rain_word([h[i] for h in halves]) for i in range(n_town)]
        wet = sorted((w for w in town if w), key=phenomena.RAIN_RANK.get)
        if len(wet) * 2 < n_town:
            common = None
        else:
            common = wet[len(wet) - (n_town + 1) // 2]    # reached by at least half
        local = wet[-1] if wet else None
        if clauses and (clauses[-1][1], clauses[-1][2]) == (common, local):
            clauses[-1][0].append(date)
        else:
            clauses.append(([date], common, local))
    words = []
    for dates, common, local in clauses:
        if common is None and local is None:
            continue
        head = f"{_date_span(dates)}有{common}" if common else f"{_date_span(dates)}局地有{local}"
        if common and local and phenomena.RAIN_RANK[local] > phenomena.RAIN_RANK[common]:
            head += f"，局地{local}"
        words.append(head)
    if not words:
        parts.append("之后无明显降水")
    else:
        parts.append("之后" + "；".join(words[:3]))

    his = [q["county"]["temp_max"] for q in periods if q["kind"] == "day" and q["county"]["temp_max"] is not None]
    los = [q["county"]["temp_min"] for q in periods if q["kind"] == "night" and q["county"]["temp_min"] is not None]
    if his and los:
        parts.append(f"全县最高气温{max(his):.0f}℃，夜间最低{min(los):.0f}℃")
    text = "；".join(parts) + "。"
    kinds = [k for k in dict.fromkeys(a["type"] for a in alerts)]
    if kinds:
        text += "注意防范" + "、".join(kinds) + "。"
    return text


# ---------------------------------------------------------------- network wrapper

def choose_run(sources: tuple[str, ...], issue_utc, *, n_days: int, tz: float, sess,
               min_age_hours: float | None = None):
    """Newest cycle every source publishes deep enough for the planned periods.

    Falls back to the newest cycle that reaches :data:`periods.MAX_LEAD_H` (the
    plan is then shortened) when no cycle covers every period.
    """
    kw = {"min_age_hours": min_age_hours} if min_age_hours is not None else {}
    cands = REGISTRY[sources[0]].candidate_runs(**kw)
    fallback = None
    for run in cands:
        full = periods_mod.plan(run.init_time.replace(tzinfo=None), issue_utc, tz=tz, n_days=n_days,
                                max_lead=10**6)
        if not full:
            continue
        depth = full[-1].end_lead
        if depth <= periods_mod.MAX_LEAD_H and all(REGISTRY[s].probe_run(sess, run, depth) for s in sources):
            return run, full
        if fallback is None and all(REGISTRY[s].probe_run(sess, run, periods_mod.MAX_LEAD_H) for s in sources):
            short = periods_mod.plan(run.init_time.replace(tzinfo=None), issue_utc, tz=tz, n_days=n_days)
            if short:
                fallback = (run, short)
    if fallback:
        return fallback
    raise RuntimeError(f"no recent cycle of {sources} reaches the forecast periods")


def compute_bundle(points: list[Township], *, county: str, seat: str, days: int = 5, every: int = 3,
                   sources: tuple[str, ...] = ("ecmwf", "gfs"), member: str = "blend",
                   weights: dict[str, float] | None = None, tz: float = daily_mod.TZ_CHINA,
                   pad: float = 0.75, want_pop: bool = True, workers: int = 4,
                   min_age_hours: float | None = None, run=None, issue_utc=None, sess=None,
                   hourly: bool = False) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """``(product, hourly)`` for one cycle; ``hourly`` is None unless requested.

    ``issue_utc`` (naive UTC, default now) decides the first period.
    """
    sess = sess or pipeline.session()
    issue_utc = issue_utc or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    if run is None:
        run, plan = choose_run(sources, issue_utc, n_days=days, tz=tz, sess=sess, min_age_hours=min_age_hours)
    else:
        plan = periods_mod.plan(run.init_time.replace(tzinfo=None), issue_utc, tz=tz, n_days=days)
    steps = periods_mod.steps_for(plan, step=every)
    cfg = downscale.DownscaleConfig()
    member_ds = pipeline.forecast(points, steps=steps, sources=sources, pad=pad,
                                  weights=weights, cfg=cfg, sess=sess, run=run)
    if member not in member_ds:
        raise KeyError(f"member {member!r} not produced; have {sorted(member_ds)}")
    chosen = member_ds[member]

    ens = ens_run = None
    if want_pop:
        ens_steps = list(range(6, plan[-1].end_lead + 1, 6))
        kw = {"min_age_hours": min_age_hours} if min_age_hours else {}
        # the ensemble may be a newer cycle; it needs to reach the same valid time
        for c in gefs_mod.candidate_runs(**kw):
            need = plan[-1].end_lead + int((run.init_time - c.init_time).total_seconds() // 3600)
            if need > 0 and gefs_mod.probe_run(sess, c, need + (-need) % 6):
                ens_run = c
                ens_steps = list(range(6, need + (-need) % 6 + 1, 6))
                break
        if ens_run is not None:
            ens = gefs_mod.fetch_ensemble(points, ens_run, ens_steps, sess=sess, max_workers=workers)

    prod = build(chosen, periods=plan, issue_utc=issue_utc, county=county, seat=seat, run=str(run),
                 member=member, sources=sources, weights=weights, tz=tz, ens=ens,
                 ens_run=str(ens_run) if ens_run else None)
    if not hourly:
        return prod, None

    shape = None
    if "gfs-0p25" in member_ds:
        extra_hours = hourly_mod.shape_hours(steps, limit=gfs_mod_det.HOURLY_LIMIT_H)
        lat_min, lat_max, lon_min, lon_max = pipeline.bbox_of(points, pad)
        bbox = (lat_min, lat_max, lon_min, lon_max) if lon_min <= lon_max else None
        grid = gfs_mod_det.fetch(run, extra_hours, sess=sess, bbox=bbox, lean=True,
                                 workers=max(1, min(workers, 2)))
        if bbox is None:
            grid = grid.bbox(lat_min, lat_max, lon_min, lon_max)
        shape = hourly_mod.merge_shape(member_ds["gfs-0p25"], downscale.apply(grid, points, cfg))
    hr = hourly_mod.disaggregate(chosen, shape, limit=gfs_mod_det.HOURLY_LIMIT_H)
    hpop = None
    if ens is not None:
        hpop = hourly_mod.hourly_pop(ens, np.datetime64(str(chosen.attrs["init_time"]), "h"), hr["step"].values)
    hdoc = hourly_mod.build(hr, county=county, seat=seat, run=str(run), tz=tz, pop=hpop,
                            pop_members=len(ens.members) if ens is not None else 0,
                            pop_run=str(ens_run) if ens is not None else None)
    return prod, hdoc


def compute(points: list[Township], **kw) -> dict[str, Any]:
    """The product alone (see :func:`compute_bundle`)."""
    return compute_bundle(points, **{**kw, "hourly": False})[0]
