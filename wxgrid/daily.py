"""Collapse a 3-hourly township forecast into local calendar days.

Outputs the four things a county forecast bulletin actually states, per
township per day:

* ``tmin`` / ``tmax`` — daily extremes, taken from the model's own 3-hour
  window extremes (``tmin3``/``tmax3``) rather than by sampling a 3-hourly
  instantaneous field, which flattens the diurnal range.
* ``tavg`` — mean of the 3-hourly temperatures.
* ``precip`` — total, summed from the per-step increments.
* Wind — mean and maximum speed, plus the *vector-mean* direction (averaging
  angles arithmetically is wrong across the 0/360 seam).

Days are cut on local time (UTC+8 by default) and the first day is partial
whenever the cycle runs after 00 local; ``hours`` reports the coverage so a
partial day is never mistaken for a full one.
"""

from __future__ import annotations

import numpy as np
import xarray as xr

TZ_CHINA = 8.0


def _day_index(valid_time: np.ndarray, tz_hours: float) -> np.ndarray:
    """Local calendar day as days since 1970-01-01, i.e. already the date label."""
    utc_hours = valid_time.astype("datetime64[s]").astype("int64") // 3600
    return ((utc_hours + int(round(tz_hours * 3600)) // 3600) // 24).astype(int)


def to_daily(ds: xr.Dataset, *, tz_hours: float = TZ_CHINA, hours_per_step: int | None = None) -> xr.Dataset:
    """Aggregate a (point, step) township dataset to (point, day)."""
    if "valid_time" not in ds.coords:
        raise ValueError("dataset has no valid_time coordinate")
    steps = ds["step"].values.astype(int)
    if hours_per_step is None:
        hours_per_step = int(np.median(np.diff(steps))) if len(steps) > 1 else 3

    days = _day_index(ds["valid_time"].values, tz_hours)
    uniq = np.unique(days)
    label = np.array([np.datetime64(int(d), "D") for d in uniq], dtype="datetime64[D]")

    def by_day(arr: np.ndarray, how: str) -> np.ndarray:
        out = np.full((arr.shape[0], uniq.size), np.nan)
        for j, d in enumerate(uniq):
            sel = arr[:, days == d]
            if sel.size == 0:
                continue
            out[:, j] = {
                "max": np.nanmax, "min": np.nanmin, "mean": np.nanmean, "sum": np.nansum,
            }[how](sel, axis=1)
        return out

    if "tmax3" in ds:
        tmax = by_day(ds["tmax3"].values.astype(float), "max")
    else:
        tmax = by_day(ds["t2m"].values.astype(float), "max")
    if "tmin3" in ds:
        tmin = by_day(ds["tmin3"].values.astype(float), "min")
    else:
        tmin = by_day(ds["t2m"].values.astype(float), "min")
    tmax = np.maximum(tmax, tmin)  # never invert under downscaling/clamping

    u, v = ds["u10"].values.astype(float), ds["v10"].values.astype(float)
    sampled = np.hypot(u, v)
    # A gust field beats the max of 3-hourly means; fall back to the latter.
    ws = ds["gust"].values.astype(float) if "gust" in ds else sampled
    day_wind_dir = np.full((tmin.shape[0], uniq.size), np.nan)
    for j, d in enumerate(uniq):
        sel = days == d
        if not sel.any():
            continue
        du, dv = u[:, sel].mean(axis=1), v[:, sel].mean(axis=1)
        day_wind_dir[:, j] = (270.0 - np.degrees(np.arctan2(dv, du))) % 360.0

    coverage = np.array([hours_per_step * int((days == d).sum()) for d in uniq], dtype=float)

    out = xr.Dataset(
        {
            "tmax": (("point", "day"), tmax, {"units": "degC"}),
            "tmin": (("point", "day"), tmin, {"units": "degC"}),
            "tavg": (("point", "day"), by_day(ds["t2m"].values.astype(float), "mean"), {"units": "degC"}),
            "precip": (("point", "day"), by_day(ds["precip"].values.astype(float), "sum"), {"units": "mm"}),
            "snow": (("point", "day"), by_day(ds["snow"].values.astype(float), "sum")
                     if "snow" in ds else np.zeros_like(tmax), {"units": "mm"}),
            "cloud": (("point", "day"), by_day(ds["cloud"].values.astype(float), "mean")
                      if "cloud" in ds else np.zeros_like(tmax), {"units": "1"}),
            "wind_speed": (("point", "day"), by_day(sampled, "mean"), {"units": "m/s"}),
            "wind_speed_min": (("point", "day"), by_day(sampled, "min"), {"units": "m/s"}),
            "wind_speed_max": (("point", "day"), by_day(sampled, "max"), {"units": "m/s"}),
            "wind_gust": (("point", "day"), by_day(ws, "max"), {"units": "m/s"}),
            "wind_dir": (("point", "day"), day_wind_dir, {"units": "degree", "comment": "vector mean, blows FROM"}),
        },
        coords={"point": ds["point"], "day": label, "hours": ("day", coverage)},
        attrs=dict(ds.attrs),
    )
    for c in ("latitude", "longitude", "elevation", "model_elevation", "name"):
        if c in ds:
            out = out.assign_coords({c: ds[c]})
    return out


def county_summary(daily: xr.Dataset) -> xr.Dataset:
    """Space-aggregate to one row per day, for the bulletin headline."""
    return xr.Dataset(
        {
            "tmax_max": daily["tmax"].max("point"),
            "tmax_mean": daily["tmax"].mean("point"),
            "tmin_min": daily["tmin"].min("point"),
            "tmin_mean": daily["tmin"].mean("point"),
            "tavg": daily["tavg"].mean("point"),
            "precip_max": daily["precip"].max("point"),
            "precip_mean": daily["precip"].mean("point"),
            "wind_mean": daily["wind_speed"].mean("point"),
            "wind_max": daily["wind_gust"].max("point"),
        },
        coords={"day": daily["day"], "hours": daily["hours"]},
    )


def compass(deg: float) -> str:
    """16-point Chinese compass name."""
    names = ["北", "北东北", "东北", "东东北", "东", "东东南", "东南", "南东南",
             "南", "南西南", "西南", "西西南", "西", "西西北", "西北", "北西北"]
    return names[int((deg % 360) / 22.5 + 0.5) % 16]


#: Local-hour boundaries of the two halves a Chinese bulletin reports separately.
DAY_START_H, NIGHT_START_H = 8, 20


def _half_of(valid_time: np.ndarray, tz_hours: float, kind: str, gap: int) -> tuple[np.ndarray, np.ndarray]:
    """``(day_index, mask)`` for the local day- or night-time half.

    A step's value covers the window ``(valid_time - gap, valid_time]``, so the
    half it belongs to is decided by where that window *sits*, i.e. its start
    hour ``local_end - gap`` — not its end. Keying on the end put the 05→08
    window in the daytime and the 17→20 window in the night.
    """
    local_end = valid_time.astype("datetime64[s]").astype("int64") // 3600 + int(round(tz_hours * 3600)) // 3600
    local_start = local_end - gap
    day = local_start // 24
    hour = local_start % 24
    if kind == "day":
        return day, (hour >= DAY_START_H) & (hour < NIGHT_START_H)
    # The night that belongs to date D runs D 20:00 -> D+1 08:00.
    return np.where(hour < DAY_START_H, day - 1, day), (hour >= NIGHT_START_H) | (hour < DAY_START_H)


def half_day(ds: xr.Dataset, *, tz_hours: float = TZ_CHINA) -> xr.Dataset:
    """Day/night wind-down of the same local days as :func:`to_daily`.

    A bulletin says "白天多云转小雨，夜间中雨", so precipitation, snowfall and
    cloud cover have to be split at 08:00 and 20:00 local, not averaged into a
    single daily number.
    """
    vt = ds["valid_time"].values
    steps = ds["step"].values.astype(int)
    gap = int(np.median(np.diff(steps))) if len(steps) > 1 else 3
    base = to_daily(ds, tz_hours=tz_hours)
    days = base["day"].values.astype("datetime64[D]").astype(int)
    out = {}
    for half in ("day", "night"):
        idx, mask = _half_of(vt, tz_hours, half, gap)
        for var, how in (("precip", "sum"), ("snow", "sum"), ("cloud", "mean")):
            if var not in ds:
                out[f"{half}_{var}"] = np.zeros((ds.sizes["point"], days.size))
                continue
            arr = np.full((ds.sizes["point"], days.size), np.nan)
            for k, d in enumerate(days):
                sel = mask & (idx == d)
                if sel.any():
                    vals = ds[var].values[:, sel].astype(float)
                    arr[:, k] = np.nansum(vals, axis=1) if how == "sum" else np.nanmean(vals, axis=1)
            out[f"{half}_{var}"] = arr
    return xr.Dataset(
        {k: (("point", "day"), v) for k, v in out.items()},
        coords={"point": base["point"], "day": base["day"]},
    )


def precip_windows(ds: xr.Dataset, *, tz_hours: float = TZ_CHINA,
                   threshold_mm: float = 0.1) -> dict[tuple[str, str], list[tuple[int, int]]]:
    """``{(point_id, 'YYYY-MM-DD'): [(start_h, end_h), ...]}`` in local hours.

    Each 3-hourly increment covers ``(step-gap, step]`` hours after init, so a
    window is the local time span of the steps that actually carry rain. A span
    that crosses local midnight is split so every day it touches lists it.
    """
    steps = ds["step"].values.astype(int)
    gap = int(np.median(np.diff(steps))) if len(steps) > 1 else 3
    if "init_time" not in ds.attrs:
        raise ValueError("dataset is missing the init_time attribute")
    offset = int(round(tz_hours * 3600)) // 3600
    init_h = int(np.asarray(ds.attrs["init_time"]).astype("datetime64[h]").astype(int))
    rate = ds["precip"].values.astype(float)

    out: dict[tuple[str, str], list[tuple[int, int]]] = {}
    for i, pid in enumerate(ds["point"].values):
        spans = sorted(
            (init_h + step - gap + offset, init_h + step + offset)
            for j, step in enumerate(steps) if rate[i, j] >= threshold_mm
        )
        merged: list[list[int]] = []
        for lo, hi in spans:
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        for lo, hi in merged:
            a = int(lo)
            while a < hi:  # split at local midnight so each day lists its own hours
                d = a // 24
                b = min(int(hi), (d + 1) * 24)
                key = str(np.datetime64(int(d), "D"))
                out.setdefault((str(pid), key), []).append((a - d * 24, b - d * 24))
                a = b
    return out


def format_windows(spans: list[tuple[int, int]] | None) -> str:
    """``20—24时`` / ``02—05时、14—17时`` — hours are within one local day (end up to 24)."""
    if not spans:
        return "无"
    return "、".join(f"{start:02d}—{end:02d}时" for start, end in spans)
