"""降水概率 from the GEFS ensemble, on local calendar days.

GEFS precipitation arrives as 6-hour buckets that tile from init, so a bucket
straddling local midnight is split by its overlap with each day. Probability is
then simply the share of members that put at least a trace of precipitation in
that window — no fitting, nothing to calibrate away.

Two knobs matter and are exposed rather than hidden:

* ``threshold_mm`` — the "does it rain at all" cut. 0.1 mm is the CMA trace
  threshold and matches the 小雨/中雨 grading used elsewhere.
* ``n_members`` — probability resolution is 100 %/n; with the default 21 members
  the output quantises to ~5 %, which is why :func:`wxgrid.phenomena.pop_text`
  rounds to 10 %.
"""

from __future__ import annotations

import numpy as np
import xarray as xr

from .sources.gefs import EnsemblePrecip

TRACE_MM = 0.1


def local_day_bounds(init_time: np.datetime64, days: np.ndarray, tz_hours: float) -> np.ndarray:
    """``(n, 2)`` array of [start, end) hours after init for each local day."""
    init_h = init_time.astype("datetime64[h]").astype(int)
    starts = days.astype("datetime64[D]").astype("datetime64[h]").astype(int) - int(tz_hours) - init_h
    return np.stack([starts, starts + 24], axis=1)


def window_pop(ens: EnsemblePrecip, start_utc_h: np.ndarray, end_utc_h: np.ndarray, *,
               threshold_mm: float = TRACE_MM) -> np.ndarray:
    """``(point, window)`` % of members with >= ``threshold_mm`` in each window.

    Windows are absolute UTC hours (hours since the epoch), so the ensemble may be
    on another cycle than the deterministic run. A window the ensemble does not
    fully cover is NaN rather than a probability over part of it.
    """
    init_h = int(np.datetime64(ens.init_time, "h").astype(int))
    bounds = np.stack([np.asarray(start_utc_h) - init_h, np.asarray(end_utc_h) - init_h], axis=1)
    totals = _member_daily(ens, bounds)  # (member, point, window)
    covered = np.zeros(bounds.shape[0])
    for j in range(ens.mm.shape[2]):
        b0, b1 = float(ens.starts_h[j]), float(ens.ends_h[j])
        covered += np.clip(np.minimum(b1, bounds[:, 1]) - np.maximum(b0, bounds[:, 0]), 0, None)
    out = (totals >= threshold_mm).mean(axis=0) * 100.0
    out[:, covered < (bounds[:, 1] - bounds[:, 0]) - 1e-6] = np.nan
    return out


def _member_daily(ens: EnsemblePrecip, bounds: np.ndarray) -> np.ndarray:
    """``(member, point, day)`` daily totals, splitting straddling buckets."""
    n_day = bounds.shape[0]
    out = np.zeros((ens.mm.shape[0], ens.mm.shape[1], n_day))
    width = np.maximum(ens.ends_h - ens.starts_h, 1).astype(float)
    for j in range(ens.mm.shape[2]):
        b0, b1 = float(ens.starts_h[j]), float(ens.ends_h[j])
        for k in range(n_day):
            overlap = max(0.0, min(b1, bounds[k, 1]) - max(b0, bounds[k, 0]))
            if overlap > 0:
                out[:, :, k] += ens.mm[:, :, j] * (overlap / width[j])
    return out


def daily_pop(ens: EnsemblePrecip, day: np.ndarray, tz_hours: float,
              *, threshold_mm: float = TRACE_MM) -> xr.DataArray:
    """Precipitation probability in percent, dims ``(point, day)``."""
    bounds = local_day_bounds(ens.init_time, day, tz_hours)
    member_daily = _member_daily(ens, bounds)
    wet = (member_daily >= threshold_mm).mean(axis=0) * 100.0
    return xr.DataArray(
        wet, dims=("point", "day"),
        coords={"point": list(ens.point_ids), "day": day},
        attrs={"units": "%", "long_name": "probability of >=%g mm in the local day" % threshold_mm,
               "members": len(ens.members)},
    )


def ensemble_daily_total(ens: EnsemblePrecip, day: np.ndarray, tz_hours: float) -> xr.DataArray:
    """Per-member daily totals, dims ``(member, point, day)`` — keep for spread/percentiles."""
    bounds = local_day_bounds(ens.init_time, day, tz_hours)
    return xr.DataArray(
        _member_daily(ens, bounds), dims=("member", "point", "day"),
        coords={"member": list(ens.members), "point": list(ens.point_ids), "day": day},
        attrs={"units": "mm"},
    )
