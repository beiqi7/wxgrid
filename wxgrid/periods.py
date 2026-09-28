"""白天 / 夜间 periods — the unit a Chinese public forecast is written in.

A forecast day is two periods, in local time:

* 白天 08:00–20:00 of date D — carries the day's **maximum** temperature;
* 夜间 20:00 of D – 08:00 of D+1 — carries the night's **minimum** temperature.

Weather, wind and precipitation are stated for each period separately.

The product starts from the period the issue time falls in, or from the next one
when less than :data:`MIN_REMAINING_H` of it is left. So a product issued at 07:30
opens with 今天白天 and one issued at 19:30 with 今天夜间, and five full days follow.

Every 3-hourly model step covers the window ``(step - 3, step]``; a window belongs
to the period its *start* falls in (as in :func:`wxgrid.daily._half_of`), so each
period is exactly four windows: 08-11 … 17-20 for 白天, 20-23 … 05-08 for 夜间.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass

import numpy as np
import xarray as xr

DAY_START_H, NIGHT_START_H = 8, 20
PERIOD_H = 12
#: Start at the next period when less than this much of the current one remains.
MIN_REMAINING_H = 6
#: ECMWF IFS open data is 3-hourly to 144 h on the 00/12 UTC cycles.
MAX_LEAD_H = 144

WEEKDAY = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


@dataclass(frozen=True)
class Period:
    kind: str          # "day" (白天) | "night" (夜间)
    date: str          # local date the period is named after, YYYY-MM-DD
    start_lead: int    # hours after model init, inclusive
    end_lead: int      # hours after model init, exclusive
    start_local: str   # YYYY-MM-DDTHH:00, local
    end_local: str

    @property
    def name(self) -> str:
        return "白天" if self.kind == "day" else "夜间"

    def as_dict(self) -> dict:
        return asdict(self)


def _hours(ts) -> float:
    """Hours since the epoch for a naive-UTC datetime64/str/datetime."""
    return float(np.datetime64(ts, "s").astype("int64")) / 3600.0


def _stamp(local_h: int) -> str:
    return str(np.datetime64(int(local_h), "h"))[:13] + ":00"


def plan(init_utc, issue_utc, *, tz: float = 8.0, n_days: int = 5,
         max_lead: int = MAX_LEAD_H) -> list[Period]:
    """The periods to forecast for a run started at ``init_utc`` and issued at ``issue_utc``.

    Five full days are ``2 * n_days`` periods from a 白天 start and ``2 * n_days + 1``
    from a 夜间 start (tonight, then five whole days). Periods that start before
    the model or end after ``max_lead`` are dropped rather than half-filled.
    """
    off = int(round(tz))
    init_l = int(round(_hours(init_utc))) + off
    issue_l = _hours(issue_utc) + off
    start = int(np.floor((issue_l - DAY_START_H) / PERIOD_H)) * PERIOD_H + DAY_START_H
    if start + PERIOD_H - issue_l < MIN_REMAINING_H:
        start += PERIOD_H
    first_kind = "day" if start % 24 == DAY_START_H else "night"
    n = 2 * n_days + (1 if first_kind == "night" else 0)

    out: list[Period] = []
    for k in range(n):
        s = start + PERIOD_H * k
        e = s + PERIOD_H
        sl, el = s - init_l, e - init_l
        if sl < 0:
            continue
        if el > max_lead:
            break
        out.append(Period(
            kind="day" if s % 24 == DAY_START_H else "night",
            date=str(np.datetime64(s // 24, "D")),
            start_lead=sl, end_lead=el, start_local=_stamp(s), end_local=_stamp(e),
        ))
    return out


def steps_for(periods: list[Period], *, step: int = 3) -> list[int]:
    """Model steps to fetch: every step from the first one to the end of the last period.

    Starting at the first step (not at the first period) keeps the precipitation
    increments right — accumulations are differenced step to step from init — and
    covers the 3-hourly series from the issue time. The extra early steps are a
    handful of small reads.
    """
    if not periods:
        raise ValueError("no periods to forecast")
    return list(range(step, periods[-1].end_lead + 1, step))


def relative_label(date: str, issue_date: str) -> str:
    """昨天 / 今天 / 明天 / 后天, else the weekday."""
    d = (np.datetime64(date, "D") - np.datetime64(issue_date, "D")).astype(int)
    if d in (-1, 0, 1, 2):
        return {-1: "昨天", 0: "今天", 1: "明天", 2: "后天"}[int(d)]
    return WEEKDAY[np.datetime64(date, "D").astype(dt.date).weekday()]


def period_label(date: str, kind: str, issue_date: str) -> str:
    """``今天夜间`` / ``明天白天`` …; last night's remainder, issued after midnight, is ``今天凌晨``."""
    rel = relative_label(date, issue_date)
    if rel == "昨天" and kind == "night":
        return "今天凌晨"
    return f"{rel}{'白天' if kind == 'day' else '夜间'}"


def aggregate(ds: xr.Dataset, periods: list[Period]) -> xr.Dataset:
    """(point, step) township dataset -> (point, period) half-day aggregates.

    Temperatures use the model's own 3-hour window extremes (``tmax3``/``tmin3``)
    when present. Wind speed range and direction come from the instantaneous 10 m
    wind at the window ends; gust is the maximum. A period missing any of its
    windows is all-NaN rather than silently computed from a partial set.
    """
    steps = ds["step"].values.astype(int)
    gap = int(np.median(np.diff(steps))) if len(steps) > 1 else 3
    n_pt, n_pd = ds.sizes["point"], len(periods)
    want = PERIOD_H // gap

    def col(var, fallback=None):
        if var in ds:
            return ds[var].transpose("point", "step").values.astype(float)
        return None if fallback is None else col(fallback)

    t2m = col("t2m")
    tmax3, tmin3 = col("tmax3", "t2m"), col("tmin3", "t2m")
    precip = col("precip")
    snow = col("snow")
    cloud = col("cloud")
    u, v = col("u10"), col("v10")
    gust = col("gust")
    speed = np.hypot(u, v)

    names = ("tmax", "tmin", "tmean", "precip", "snow", "cloud", "wind_speed_min",
             "wind_speed_max", "wind_speed", "wind_dir", "gust", "precip_3h_max")
    out = {k: np.full((n_pt, n_pd), np.nan) for k in names}
    for k, p in enumerate(periods):
        sel = (steps - gap >= p.start_lead) & (steps <= p.end_lead)
        if sel.sum() != want:
            continue
        out["tmax"][:, k] = tmax3[:, sel].max(axis=1)
        out["tmin"][:, k] = tmin3[:, sel].min(axis=1)
        out["tmean"][:, k] = t2m[:, sel].mean(axis=1)
        out["precip"][:, k] = precip[:, sel].sum(axis=1)
        out["precip_3h_max"][:, k] = precip[:, sel].max(axis=1)
        out["snow"][:, k] = snow[:, sel].sum(axis=1) if snow is not None else 0.0
        out["cloud"][:, k] = cloud[:, sel].mean(axis=1) if cloud is not None else 0.0
        out["wind_speed_min"][:, k] = speed[:, sel].min(axis=1)
        out["wind_speed_max"][:, k] = speed[:, sel].max(axis=1)
        out["wind_speed"][:, k] = speed[:, sel].mean(axis=1)
        mu, mv = u[:, sel].mean(axis=1), v[:, sel].mean(axis=1)
        out["wind_dir"][:, k] = (270.0 - np.degrees(np.arctan2(mv, mu))) % 360.0
        out["gust"][:, k] = gust[:, sel].max(axis=1) if gust is not None else speed[:, sel].max(axis=1)
    out["tmax"] = np.maximum(out["tmax"], out["tmin"])  # never invert under downscaling

    agg = xr.Dataset({k: (("point", "period"), a) for k, a in out.items()},
                     coords={"point": ds["point"].values, "period": np.arange(n_pd)},
                     attrs=dict(ds.attrs))
    for c in ("latitude", "longitude", "elevation", "model_elevation", "name"):
        if c in ds.coords:
            agg = agg.assign_coords({c: ("point", ds[c].values)})
    return agg
