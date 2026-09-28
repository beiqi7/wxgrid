"""Hourly township series: the 3-hourly blend as the level, GFS as the hourly shape.

ECMWF IFS open data is 3-hourly; GFS 0.25 deg is hourly out to 120 h. Blending
only at the common 3-hourly steps and then interpolating would throw away the
diurnal curve and the timing of showers inside each 3-hour window. Using GFS
alone would throw away the blend. So the hourly series keeps the blend as the
level and borrows only the *shape within each window* from GFS:

Instantaneous fields (``t2m``, ``u10``, ``v10``, ``cloud``, ``gust``)::

    X(h) = B_lin(h) + [G(h) - G_lin(h)]

``B_lin`` / ``G_lin`` are straight lines between the 3-hourly nodes of the blend
and of GFS. At a node ``X == B`` exactly, so the hourly and the daily product
agree wherever they overlap; between nodes, X bends the way GFS bends. The
elevation correction is constant in time, so it passes through unchanged.

Accumulated fields (``precip``, ``snow``) in a window ``(n0, n1]``::

    P(h) = B(n1) * g(h) / sum(g over the window)

with ``g`` the GFS hourly increments; equal shares when GFS is dry there. Window
totals are conserved exactly — the hourly rain adds up to the 3-hourly rain.

Without a shape source (no GFS in the run) the same code degrades to linear
interpolation and equal precipitation shares.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import xarray as xr

from . import phenomena

#: Instantaneous variables carried to hourly. Wind is done on u/v, not speed.
_INSTANT = ("t2m", "u10", "v10", "cloud", "gust")
#: Accumulated per-step variables distributed by the GFS hourly shape.
_ACCUM = ("precip", "snow")


def shape_hours(node_steps, *, limit: int) -> list[int]:
    """Lead hours the shape source must add to the nodes it already has.

    Covers the first window too (hours before the first node) so its precipitation
    share is right, and stops at ``limit`` (GFS is hourly only to 120 h).
    """
    nodes = sorted({int(s) for s in node_steps})
    if not nodes:
        return []
    gap0 = nodes[1] - nodes[0] if len(nodes) > 1 else nodes[0]
    lo = max(1, nodes[0] - gap0 + 1)
    hi = min(nodes[-1], limit)
    have = set(nodes)
    return [h for h in range(lo, hi + 1) if h not in have]


def merge_shape(nodes: xr.Dataset, extra: xr.Dataset | None) -> xr.Dataset:
    """One (point, step) shape dataset from the source's node steps + its extra hours."""
    keep = [v for v in (*_INSTANT, "precip_accum") if v in nodes]
    if extra is None:
        return nodes[keep]
    both = [v for v in keep if v in extra]
    cat = xr.concat([nodes[both], extra[both]], dim="step", coords="minimal",
                    compat="override", join="exact")
    return cat.sortby("step")


def _at(ds: xr.Dataset | None, var: str, steps: np.ndarray, n_point: int) -> np.ndarray:
    """``ds[var]`` on ``steps`` as a (point, step) array, NaN where absent."""
    if ds is None or var not in ds:
        return np.full((n_point, steps.size), np.nan)
    da = ds[var].transpose("point", "step")
    have = {int(s): j for j, s in enumerate(ds["step"].values)}
    vals = da.values.astype(float)
    out = np.full((n_point, steps.size), np.nan)
    for k, s in enumerate(steps):
        j = have.get(int(s))
        if j is not None:
            out[:, k] = vals[:, j]
    return out


def disaggregate(blend: xr.Dataset, shape: xr.Dataset | None = None, *,
                 limit: int | None = None) -> xr.Dataset:
    """Hourly (point, step) dataset from a 3-hourly (point, step) blend.

    ``shape`` is an hourly (point, step) dataset of the same points — normally the
    downscaled GFS member, nodes and in-between hours merged. Output runs from the
    first node to ``min(last node, limit)``.
    """
    nodes = np.array(sorted(int(s) for s in blend["step"].values))
    if nodes.size == 0:
        raise ValueError("blend has no steps")
    if shape is not None and not np.array_equal(shape["point"].values, blend["point"].values):
        raise ValueError("shape and blend cover different points")
    end = int(nodes[-1] if limit is None else min(nodes[-1], limit))
    hours = np.arange(int(nodes[0]), end + 1)
    npt = blend.sizes["point"]
    b = blend.transpose("point", "step")

    # window bookkeeping: each hour sits in (nodes[i1-1], nodes[i1]]
    i1 = np.searchsorted(nodes, hours, side="left")
    i0 = np.maximum(i1 - 1, 0)
    n0, n1 = nodes[i0], nodes[i1]
    at_node = n1 == hours
    frac = np.where(at_node, 1.0, (hours - n0) / np.maximum(n1 - n0, 1))

    def lin(node_vals: np.ndarray) -> np.ndarray:
        a0, a1 = node_vals[:, i0], node_vals[:, i1]
        return np.where(at_node, a1, a0 + (a1 - a0) * frac)

    out: dict[str, np.ndarray] = {}
    for var in _INSTANT:
        if var not in b:
            continue
        base = lin(b[var].values.astype(float))
        g_h = _at(shape, var, hours, npt)
        g_lin = lin(_at(shape, var, nodes, npt))
        dev = np.where(at_node, 0.0, np.nan_to_num(g_h - g_lin, nan=0.0))
        out[var] = base + dev
    if "cloud" in out:
        out["cloud"] = np.clip(out["cloud"], 0.0, 1.0)

    # hourly GFS increments from its since-init accumulation, hours 1..end
    acc = _at(shape, "precip_accum", np.arange(0, end + 1), npt)
    acc[:, 0] = 0.0  # nothing has accumulated at init
    inc = np.clip(np.diff(acc, axis=1), 0.0, None)  # inc[:, h-1] covers (h-1, h]
    gap0 = int(nodes[1] - nodes[0]) if nodes.size > 1 else int(nodes[0])
    shares = np.full((npt, hours.size), np.nan)
    for i, n in enumerate(nodes):
        if n > end:
            break
        prev = int(nodes[i - 1]) if i else int(n) - gap0
        win = np.arange(max(prev, 0) + 1, int(n) + 1)
        g = inc[:, win - 1]
        tot = g.sum(axis=1)
        good = np.isfinite(tot) & (tot > 1e-6)
        share = np.where(good[:, None], g / np.where(good, tot, 1.0)[:, None], 1.0 / win.size)
        sel = win >= hours[0]
        shares[:, win[sel] - hours[0]] = share[:, sel]
    for var in _ACCUM:
        if var in b:
            node_amt = b[var].values.astype(float)
            out[var] = node_amt[:, i1] * shares

    if "u10" in out and "v10" in out:
        u, v = out["u10"], out["v10"]
        out["wind_speed"] = np.hypot(u, v)
        out["wind_dir"] = (270.0 - np.degrees(np.arctan2(v, u))) % 360.0
        if "gust" in out:
            out["gust"] = np.maximum(out["gust"], out["wind_speed"])

    init = np.datetime64(str(blend.attrs["init_time"]), "s")
    ds = xr.Dataset(
        {k: (("point", "step"), v) for k, v in out.items()},
        coords={"point": blend["point"].values, "step": hours,
                "valid_time": ("step", init + hours.astype("timedelta64[h]"))},
        attrs=dict(blend.attrs),
    )
    ds.attrs["hourly_shape"] = "gfs" if shape is not None else "linear"
    for c in ("latitude", "longitude", "elevation", "model_elevation", "name"):
        if c in blend.coords:
            ds = ds.assign_coords({c: ("point", blend[c].values)})
    return ds


def hourly_pop(ens, init_time: np.datetime64, steps: np.ndarray, *,
               threshold_mm: float = 0.1) -> np.ndarray:
    """(point, step) share of GEFS members wet in the 6 h bucket holding each hour.

    ``steps`` are lead hours of the *deterministic* run starting at ``init_time``;
    the ensemble may be on another cycle, so hours are matched on valid time.
    Hours no bucket covers are NaN.
    """
    shift = int((np.datetime64(ens.init_time, "h") - np.datetime64(init_time, "h")).astype(int))
    rel = np.asarray(steps, dtype=int) - shift  # lead hour in the ensemble's clock
    wet = (ens.mm >= threshold_mm).mean(axis=0) * 100.0  # (point, bucket)
    out = np.full((ens.mm.shape[1], rel.size), np.nan)
    for j in range(ens.mm.shape[2]):
        inside = (rel - 1 >= ens.starts_h[j]) & (rel <= ens.ends_h[j])
        out[:, inside] = wet[:, j][:, None]
    return out


def _r(a: np.ndarray, nd: int) -> list:
    """Rounded JSON list; NaN -> null."""
    return [None if not np.isfinite(x) else (round(float(x), nd) if nd else int(round(float(x))))
            for x in a]


def build(hr: xr.Dataset, *, county: str, seat: str, run: str, tz: float,
          pop: np.ndarray | None = None, pop_members: int = 0,
          pop_run: str | None = None) -> dict[str, Any]:
    """Columnar hourly JSON: one array per field per township, times listed once."""
    offset = int(round(tz * 3600)) // 3600
    local = (hr["valid_time"].values.astype("datetime64[h]") + np.timedelta64(offset, "h"))
    times = [str(t)[:13] + ":00" for t in local]
    sign = "+" if offset >= 0 else "-"
    tzs = f"{sign}{abs(offset):02d}:00"

    ids = [str(p) for p in hr["point"].values]
    points: dict[str, Any] = {}
    for i, pid in enumerate(ids):
        p = hr["precip"].values[i] if "precip" in hr else np.zeros(hr.sizes["step"])
        s = hr["snow"].values[i] if "snow" in hr else np.zeros_like(p)
        c = hr["cloud"].values[i] if "cloud" in hr else np.zeros_like(p)
        ws = hr["wind_speed"].values[i]
        wd = hr["wind_dir"].values[i]
        weather = [phenomena.hour_text(float(pp), float(ss), float(cc)) for pp, ss, cc in zip(p, s, c)]
        points[pid] = {
            "name": str(hr["name"].values[i]) if "name" in hr.coords else pid,
            "lat": round(float(hr["latitude"].values[i]), 5) if "latitude" in hr.coords else None,
            "lon": round(float(hr["longitude"].values[i]), 5) if "longitude" in hr.coords else None,
            "elevation": round(float(hr["elevation"].values[i]), 1) if "elevation" in hr.coords else None,
            "weather": weather,
            "temp": _r(hr["t2m"].values[i], 1),
            "precip": _r(p, 2),
            "snow": _r(s, 2),
            "cloud": _r(c * 100.0, 0),
            "wind_speed": _r(ws, 1),
            "wind_dir": _r(wd, 0),
            "wind_name": [phenomena.wind_name(float(d)) if np.isfinite(d) else None for d in wd],
            "wind_force": [phenomena.beaufort(float(v)) if np.isfinite(v) else None for v in ws],
            "gust": _r(hr["gust"].values[i], 1) if "gust" in hr else None,
            "pop": _r(pop[i], 0) if pop is not None else None,
        }

    shape = hr.attrs.get("hourly_shape", "linear")
    return {
        "meta": {
            "county": county, "seat": seat, "run": run,
            "init_time": str(hr.attrs.get("init_time", "")),
            "tz": tz, "tz_label": tzs, "step_hours": 1, "n_hours": len(times),
            "first_lead_h": int(hr["step"].values[0]), "last_lead_h": int(hr["step"].values[-1]),
            "source": str(hr.attrs.get("source", "")),
            "shape": shape,
            "method": ("3 小时融合值为基准，逐时形态取 GFS 逐时预报；降水按 GFS 逐时占比分配，3 小时总量守恒"
                       if shape == "gfs" else "3 小时融合值线性插值，降水在 3 小时内平均分配"),
            "units": {"temp": "degC", "precip": "mm/h", "snow": "mm/h (water equivalent)",
                      "cloud": "%", "wind_speed": "m/s", "wind_dir": "degree (from)",
                      "gust": "m/s", "pop": "% (6 h GEFS bucket holding the hour)"},
            "time_convention": "times are local hour ends; precip/snow cover the hour before",
            "pop_members": pop_members, "pop_run": pop_run,
            "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        },
        "lead_h": [int(s) for s in hr["step"].values],
        "times": times,
        "points": points,
    }
