"""Multi-model point forecasts from Open-Meteo (https://open-meteo.com, CC BY 4.0).

Open-Meteo serves the output of the national weather services' global models at
arbitrary points, with a lapse-rate correction to the requested elevation. It
is the only practical way for a small box to use models whose raw output is not
openly downloadable in bulk (UKMO, CMA GRAPES, CMC GEM, Météo-France ARPEGE) or
is too heavy to regrid here (DWD ICON's icosahedral grid).

The member set was chosen by verification (see :mod:`wxgrid.verify`): against
seven national stations around the county over 60 days, the mean of these eight
beat every single model and the previous ECMWF+GFS blend at every lead time.
GFS and JMA GSM were left out — both had clearly larger temperature errors here.

Free use is for non-commercial purposes; commercial use needs an API key.
"""
from __future__ import annotations

import json
import os
from typing import Any

import numpy as np
import xarray as xr

from ._fetch import get, session

API = os.environ.get("WXGRID_OPENMETEO_URL", "https://api.open-meteo.com/v1/forecast")
API_KEY = os.environ.get("WXGRID_OPENMETEO_KEY", "")

#: Open-Meteo model id -> display name.
MEMBERS = {
    "ecmwf_ifs": "ECMWF IFS 9 km",
    "ecmwf_aifs025_single": "ECMWF AIFS",
    "icon_global": "DWD ICON",
    "ukmo_global_deterministic_10km": "UKMO 10 km",
    "cma_grapes_global": "CMA GRAPES",
    "gem_global": "CMC GEM",
    "meteofrance_arpege_world": "Météo-France ARPEGE",
    "ncep_aigfs025": "NOAA AIGFS",
}
HOURLY = ("temperature_2m", "precipitation", "snowfall", "cloud_cover",
          "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m")
#: Fewer members than this at a step and the step is left missing.
MIN_MEMBERS = 3


def fetch(points, *, members=tuple(MEMBERS), days: int = 7, past_days: int = 1, sess=None) -> dict[str, Any]:
    """Hourly forecasts for every point and member.

    Returns ``{"time": [UTC hour strings], "members": {model: {var: (point, hour) array}}}``.
    Raises when the service is unreachable or answers with an error.
    """
    sess = sess or session()
    q = (f"{API}?latitude={','.join(f'{p.lat:.5f}' for p in points)}"
         f"&longitude={','.join(f'{p.lon:.5f}' for p in points)}"
         f"&elevation={','.join(f'{p.elevation_m:.1f}' for p in points)}"
         f"&hourly={','.join(HOURLY)}&models={','.join(members)}"
         f"&timezone=GMT&wind_speed_unit=ms&forecast_days={days}&past_days={past_days}"
         + (f"&apikey={API_KEY}" if API_KEY else ""))
    raw = json.loads(get(sess, q, timeout=120, retries=4).decode("utf-8"))
    if isinstance(raw, dict):
        if raw.get("error"):
            raise RuntimeError(f"Open-Meteo: {raw.get('reason')}")
        raw = [raw]
    if len(raw) != len(points):
        raise RuntimeError(f"Open-Meteo returned {len(raw)} locations for {len(points)} points")
    times = raw[0]["hourly"]["time"]
    out: dict[str, dict[str, np.ndarray]] = {}
    for m in members:
        per = {}
        for var in HOURLY:
            key = f"{var}_{m}" if len(members) > 1 else var
            rows = []
            for r in raw:
                col = r["hourly"].get(key)
                rows.append([np.nan if v is None else float(v) for v in col] if col else [np.nan] * len(times))
            per[var] = np.array(rows, dtype=float)
        if np.isfinite(per["temperature_2m"]).any():
            out[m] = per
    if not out:
        raise RuntimeError("Open-Meteo returned no data for any member")
    return {"time": times, "members": out}


def _member_steps(per: dict[str, np.ndarray], hour_idx: dict[int, int], init_h: int,
                  steps: np.ndarray, gap: int) -> dict[str, np.ndarray]:
    """One member's hourly series -> 3-hourly windows ending at each step.

    ``t2m``/wind/cloud are the values at the window end; ``tmax3``/``tmin3`` the
    extremes over its four hourly values (so a 12 h period's maximum covers all
    13 hours, as in the verification); precipitation, snowfall and gust are the
    window's sum / sum / maximum over the three hourly values ending in it.
    """
    n_pt = next(iter(per.values())).shape[0]
    shape = (n_pt, steps.size)
    out = {k: np.full(shape, np.nan) for k in ("t2m", "tmax3", "tmin3", "precip", "snow", "cloud",
                                                  "u10", "v10", "gust")}

    def col(var, hours):
        idx = [hour_idx.get(init_h + h) for h in hours]
        if any(i is None for i in idx):
            return None
        return per[var][:, idx]

    for k, s in enumerate(steps):
        s = int(s)
        t = col("temperature_2m", range(s - gap, s + 1))
        if t is None or not np.isfinite(t).all(axis=1).any():
            continue
        out["t2m"][:, k] = t[:, -1]
        out["tmax3"][:, k] = np.max(t, axis=1)
        out["tmin3"][:, k] = np.min(t, axis=1)
        p = col("precipitation", range(s - gap + 1, s + 1))
        out["precip"][:, k] = p.sum(axis=1)
        sn = col("snowfall", range(s - gap + 1, s + 1))
        out["snow"][:, k] = sn.sum(axis=1) * 10.0 / 7.0  # cm of snow -> mm water equivalent
        out["cloud"][:, k] = col("cloud_cover", [s])[:, 0] / 100.0
        spd, wd = col("wind_speed_10m", [s])[:, 0], col("wind_direction_10m", [s])[:, 0]
        rad = np.radians(wd)
        out["u10"][:, k] = -spd * np.sin(rad)
        out["v10"][:, k] = -spd * np.cos(rad)
        g = col("wind_gusts_10m", range(s - gap + 1, s + 1))
        out["gust"][:, k] = np.max(g, axis=1)
    return out


def member_datasets(raw: dict[str, Any], points, init_utc, steps, *, gap: int = 3) -> dict[str, xr.Dataset]:
    """Each member on the (point, step) grid of a run started at ``init_utc``."""
    init_h = int(np.datetime64(init_utc, "h").astype("int64"))
    hour_idx = {int(np.datetime64(t, "h").astype("int64")): i for i, t in enumerate(raw["time"])}
    steps = np.asarray(steps, dtype=int)
    init = np.datetime64(init_utc, "s")
    coords = {"point": [p.id for p in points], "step": steps,
              "valid_time": ("step", init + steps.astype("timedelta64[h]")),
              "latitude": ("point", [p.lat for p in points]), "longitude": ("point", [p.lon for p in points]),
              "elevation": ("point", [p.elevation_m for p in points]),
              "name": ("point", [p.name for p in points])}
    out = {}
    for name, per in raw["members"].items():
        data = _member_steps(per, hour_idx, init_h, steps, gap)
        out[name] = xr.Dataset({k: (("point", "step"), v) for k, v in data.items()}, coords=coords,
                               attrs={"init_time": str(init), "source": name})
    return out


def ensemble(raw: dict[str, Any], points, init_utc, steps, *, gap: int = 3,
             min_members: int = MIN_MEMBERS, members: dict[str, xr.Dataset] | None = None) -> xr.Dataset:
    """Equal-weight member mean on the (point, step) grid of a run started at ``init_utc``.

    Members that do not reach a step (ARPEGE stops at ~4 days, GRAPES at ~5.5)
    drop out of the mean there; with fewer than ``min_members`` the step is NaN.
    Wind is averaged as u/v, as in :mod:`wxgrid.blend`.
    """
    members = members or member_datasets(raw, points, init_utc, steps, gap=gap)
    names = list(members)
    first = next(iter(members.values()))

    def mean(var):
        stack = np.stack([m[var].values for m in members.values()])
        count = np.isfinite(stack).sum(axis=0)
        with np.errstate(invalid="ignore"):
            v = np.nanmean(stack, axis=0)
        return np.where(count >= min_members, v, np.nan), count

    data, counts = {}, None
    for var in ("t2m", "tmax3", "tmin3", "precip", "snow", "cloud", "u10", "v10", "gust"):
        data[var], c = mean(var)
        if var == "t2m":
            counts = c
    data["tmax3"] = np.maximum(data["tmax3"], data["t2m"])
    data["tmin3"] = np.minimum(data["tmin3"], data["t2m"])
    speed = np.hypot(data["u10"], data["v10"])
    data["gust"] = np.fmax(data["gust"], speed)
    ds = xr.Dataset({k: (("point", "step"), v) for k, v in data.items()}, coords=first.coords,
                    attrs={"init_time": first.attrs["init_time"],
                           "source": "multimodel(" + "+".join(names) + ")"})
    ds["wind_speed"] = (("point", "step"), speed)
    ds["wind_dir"] = (("point", "step"), (270.0 - np.degrees(np.arctan2(data["v10"], data["u10"]))) % 360.0)
    ds["members"] = (("point", "step"), counts.astype(float))
    ds.attrs["members"] = ",".join(names)
    return ds


def period_extremes(members: dict[str, xr.Dataset], periods, *,
                    min_members: int = MIN_MEMBERS) -> dict[str, np.ndarray]:
    """Member mean of each member's own period maximum / minimum, (point, period).

    Averaging extremes (not taking the extreme of the average) is what the
    verification scores, and it does not flatten a maximum that members put at
    different hours.
    """
    from .. import periods as periods_mod
    aggs = [periods_mod.aggregate(m, periods) for m in members.values()]
    out = {}
    for var in ("tmax", "tmin"):
        stack = np.stack([a[var].values for a in aggs])
        count = np.isfinite(stack).sum(axis=0)
        with np.errstate(invalid="ignore"):
            out[var] = np.where(count >= min_members, np.nanmean(stack, axis=0), np.nan)
    return out


def hourly_mean(raw: dict[str, Any], points, *, min_members: int = MIN_MEMBERS) -> dict[str, np.ndarray]:
    """Plain hourly member means (for the reference hourly API)."""
    out = {}
    for var in HOURLY:
        stack = np.stack([per[var] for per in raw["members"].values()])
        count = np.isfinite(stack).sum(axis=0)
        with np.errstate(invalid="ignore"):
            out[var] = np.where(count >= min_members, np.nanmean(stack, axis=0), np.nan)
    return out


def hourly_dataset(raw: dict[str, Any], points, init_utc, hours, *,
                   min_members: int = MIN_MEMBERS) -> xr.Dataset:
    """Hourly member mean on (point, step) for lead hours ``hours`` after ``init_utc``.

    Shaped like :func:`wxgrid.hourly.disaggregate`'s output so :func:`wxgrid.hourly.build`
    can render it. Wind is averaged as u/v.
    """
    init_h = int(np.datetime64(init_utc, "h").astype("int64"))
    hour_idx = {int(np.datetime64(t, "h").astype("int64")): i for i, t in enumerate(raw["time"])}
    hours = np.asarray([h for h in hours if init_h + int(h) in hour_idx], dtype=int)
    cols = [hour_idx[init_h + int(h)] for h in hours]

    def mean(arrs):
        stack = np.stack(arrs)[:, :, cols]
        count = np.isfinite(stack).sum(axis=0)
        with np.errstate(invalid="ignore"):
            return np.where(count >= min_members, np.nanmean(stack, axis=0), np.nan)

    members = list(raw["members"].values())
    rad = [np.radians(m["wind_direction_10m"]) for m in members]
    u = mean([-m["wind_speed_10m"] * np.sin(r) for m, r in zip(members, rad)])
    v = mean([-m["wind_speed_10m"] * np.cos(r) for m, r in zip(members, rad)])
    speed = np.hypot(u, v)
    data = {
        "t2m": mean([m["temperature_2m"] for m in members]),
        "precip": mean([m["precipitation"] for m in members]),
        "snow": mean([m["snowfall"] * 10.0 / 7.0 for m in members]),
        "cloud": mean([m["cloud_cover"] / 100.0 for m in members]),
        "gust": np.fmax(mean([m["wind_gusts_10m"] for m in members]), speed),
        "wind_speed": speed,
        "wind_dir": (270.0 - np.degrees(np.arctan2(v, u))) % 360.0,
        "u10": u, "v10": v,
    }
    init = np.datetime64(init_utc, "s")
    ds = xr.Dataset({k: (("point", "step"), a) for k, a in data.items()},
                    coords={"point": [p.id for p in points], "step": hours,
                            "valid_time": ("step", init + hours.astype("timedelta64[h]")),
                            "latitude": ("point", [p.lat for p in points]),
                            "longitude": ("point", [p.lon for p in points]),
                            "elevation": ("point", [p.elevation_m for p in points]),
                            "name": ("point", [p.name for p in points])},
                    attrs={"init_time": str(init), "source": "multimodel(" + "+".join(raw["members"]) + ")",
                           "hourly_shape": "multimodel"})
    return ds
