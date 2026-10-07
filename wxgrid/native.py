"""The native engine: raw ECMWF/NOAA model output, our own downscaling and consensus.

No intermediary service. Each model is read from its producer's open-data
archive (:mod:`wxgrid.sources.raw`), cut to the county box and archived
(:mod:`wxgrid.archive`); point values come from bilinear interpolation plus an
elevation correction; the members are combined by the station-trained
consensus in :mod:`wxgrid.consensus`.

This module holds the parts shared by training and forecasting: model values
at points, and the 白天/夜间 period values the verification scores.
"""
from __future__ import annotations

import dataclasses
import datetime as dt

import numpy as np

from .gribbox import BoxRun

#: Standard atmosphere lapse rate, K/m.
LAPSE = -0.0065
#: Cap on the elevation correction, K.
MAX_CORRECTION_K = 10.0
#: The issue time that goes with a cycle: 07:30 BJT for 12 UTC, 19:30 BJT for 00 UTC.
ISSUE_DELAY_H = 11.5
TEMP_FIELDS = ("t2m", "tmax", "tmin")


@dataclasses.dataclass(frozen=True)
class Site:
    id: str
    lat: float
    lon: float
    elevation_m: float


def sites(points) -> list[Site]:
    """Townships or stations -> :class:`Site` (anything with id/lat/lon/elevation_m)."""
    return [Site(str(p.id), float(p.lat), float(p.lon), float(p.elevation_m)) for p in points]


def point_values(run: BoxRun, pts: list[Site], *, lapse: str | float = "std") -> dict[str, np.ndarray]:
    """Each field of ``run`` at the points, ``(point, step)``; temperatures elevation-corrected.

    ``lapse`` is ``"std"`` (−6.5 K/km), ``"local"`` (the model's own lapse rate
    around the point, :meth:`BoxRun.local_lapse`, standard where the model
    terrain is too flat to tell), ``"none"``, or a number in K/m.
    """
    lat = np.array([p.lat for p in pts])
    lon = np.array([p.lon for p in pts])
    z = np.array([p.elevation_m for p in pts])
    out = {f: run.bilinear(lat, lon, v).T for f, v in run.fields.items()}
    if run.orog is None or lapse == "none":
        corr = np.zeros((len(pts), run.steps.size))
    else:
        dz = (z - run.bilinear(lat, lon, run.orog))[:, None]
        if lapse == "local":
            g = run.local_lapse()
            gp = run.bilinear(lat, lon, np.nan_to_num(g, nan=LAPSE)).T if g is not None else LAPSE
            corr = gp * dz
        else:
            corr = (LAPSE if lapse == "std" else float(lapse)) * dz * np.ones((1, run.steps.size))
        corr = np.clip(corr, -MAX_CORRECTION_K, MAX_CORRECTION_K)
    for f in TEMP_FIELDS:
        if f in out:
            out[f] = out[f] + corr
    out["elevation_correction"] = corr
    return out


# ------------------------------------------------------------------ periods (UTC, the CMA windows)

def lead_day(end: dt.datetime, issue: dt.datetime) -> int:
    """Lead day 1-5 of a period ending at ``end`` for a forecast issued at ``issue``."""
    return int(min(5, max(1, round((end - issue).total_seconds() / 86400.0))))


def period_windows(init: dt.datetime, days: int = 5):
    """``[(date, kind, start, end), ...]`` for the 白天/夜间 periods a cycle's issue covers.

    ``date`` follows :func:`wxgrid.verify.model_periods`: 白天 of D is 00–12 UTC
    on D; 夜间 keyed D is the night *ending* on D, 12 UTC D−1 to 00 UTC D.
    """
    issue = init + dt.timedelta(hours=ISSUE_DELAY_H)
    out = []
    t = dt.datetime(issue.year, issue.month, issue.day) + dt.timedelta(hours=12 * (issue.hour // 12))
    # the first period is the one the issue time falls in, or the next if < 6 h of it is left
    if (t + dt.timedelta(hours=12) - issue).total_seconds() < 6 * 3600:
        t += dt.timedelta(hours=12)
    n = 2 * days + (1 if t.hour == 12 else 0)
    for _ in range(n):
        end = t + dt.timedelta(hours=12)
        if t.hour == 0:
            out.append((t.date().isoformat(), "day", t, end))
        else:
            out.append((end.date().isoformat(), "night", t, end))
        t = end
    return out


def _valid(run: BoxRun) -> np.ndarray:
    return np.array([run.init + dt.timedelta(hours=int(s)) for s in run.steps])


def period_rows(run: BoxRun, pts: list[Site], *, lapse: str | float = "std", days: int = 5) -> list[dict]:
    """Model values per site and 白天/夜间 period, in :class:`wxgrid.verify.Matched` row form.

    * ``f_t``: 白天 maximum / 夜间 minimum — from the 3-hour window extremes where the
      model has them, else from the instantaneous 2 m temperature at the period's
      output times (AIFS: 3 samples per period);
    * ``f_p``: 12 h precipitation from the accumulation;
    * ``f_w``: strongest 10 m wind at the report hours (03/06/09/12 UTC by day,
      18/21/00 UTC by night), as :func:`wxgrid.verify.model_periods` takes it.
    """
    vals = point_values(run, pts, lapse=lapse)
    valid = _valid(run)
    issue = run.init + dt.timedelta(hours=ISSUE_DELAY_H)
    steps = run.steps
    ws = run.window_start if run.window_start is not None else steps - 3
    tp = vals.get("tp")
    speed = np.hypot(vals["u10"], vals["v10"]) if "u10" in vals and "v10" in vals else None
    t2m = vals.get("t2m")
    rows = []
    for date, kind, start, end in period_windows(run.init, days):
        s0, s1 = (start - run.init).total_seconds() / 3600, (end - run.init).total_seconds() / 3600
        if s0 < 0 or s1 > steps.max():
            continue
        f = "tmax" if kind == "day" else "tmin"
        reduce = np.max if kind == "day" else np.min
        if f in vals:
            sel = (ws >= s0) & (steps <= s1)
            # windows must tile the period exactly; an 18-24 window ending at s1 is fine
            covered = sorted({(int(a), int(b)) for a, b in zip(ws[sel], steps[sel])})
            tiles = _tiles(covered, int(s0), int(s1))
            tt = reduce(vals[f][:, sel], axis=1) if tiles else None
        else:
            tt = None
        if tt is None and t2m is not None:
            sel = (steps >= s0) & (steps <= s1)
            tt = reduce(t2m[:, sel], axis=1) if sel.sum() >= 3 else None
        pp = None
        if tp is not None:
            j0 = np.nonzero(steps == s0)[0]
            j1 = np.nonzero(steps == s1)[0]
            if j1.size:
                start_acc = tp[:, j0[0]] if j0.size else (np.zeros(len(pts)) if s0 == 0 else None)
                if start_acc is not None:
                    pp = np.clip(tp[:, j1[0]] - start_acc, 0.0, None)
        wv = None
        if speed is not None:
            hours = (3, 6, 9, 12) if kind == "day" else (6, 9, 12)
            want = [start + dt.timedelta(hours=h) for h in hours]
            sel = np.isin(valid, want)
            wv = speed[:, sel].max(axis=1) if sel.any() else None
        lead = lead_day(end, issue)
        for i, p in enumerate(pts):
            rows.append({"model": run.source, "station": p.id, "date": date, "kind": kind, "lead": lead,
                         "init": run.init.isoformat(), "end": end.isoformat(),
                         "f_t": _num(tt, i), "f_p": _num(pp, i), "f_w": _num(wv, i)})
    return rows


def _tiles(windows: list[tuple[int, int]], s0: int, s1: int) -> bool:
    """Do the (start, end] windows cover (s0, s1] without gaps (overlaps allowed)?"""
    reach = s0
    for a, b in sorted(windows):
        if a > reach:
            return False
        reach = max(reach, b)
    return reach >= s1 and bool(windows)


def _num(a, i):
    if a is None:
        return None
    v = float(a[i])
    return v if np.isfinite(v) else None
