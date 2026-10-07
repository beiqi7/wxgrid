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


def period_stats(run: BoxRun, vals: dict[str, np.ndarray], start: dt.datetime, end: dt.datetime,
                 kind: str) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None]:
    """``(temperature extreme, precipitation, max wind)`` per point over one period.

    ``vals`` is :func:`point_values` of ``run``. Any part the run cannot give
    (period outside its steps, a field it lacks) is None.

    * temperature: 白天 maximum / 夜间 minimum — from the window extremes where
      the model has them and they tile the period, else from the instantaneous
      2 m temperature at the period's output times (AIFS: 3 samples);
    * precipitation: the accumulation difference over the period;
    * wind: strongest 10 m wind at the report hours (03/06/09/12 UTC by day,
      18/21/00 UTC by night), as :func:`wxgrid.verify.model_periods` takes it.
    """
    steps = run.steps
    s0, s1 = (start - run.init).total_seconds() / 3600, (end - run.init).total_seconds() / 3600
    if s0 < 0 or s1 > steps.max():
        return None, None, None
    ws = run.window_start if run.window_start is not None else steps - 3
    f = "tmax" if kind == "day" else "tmin"
    reduce = np.max if kind == "day" else np.min
    tt = None
    if f in vals:
        sel = (ws >= s0) & (steps <= s1)
        # windows must tile the period; GFS's 18-24 window ending at s1 is fine
        if _tiles(sorted({(int(a), int(b)) for a, b in zip(ws[sel], steps[sel])}), int(s0), int(s1)):
            tt = reduce(vals[f][:, sel], axis=1)
    if tt is None and "t2m" in vals:
        sel = (steps >= s0) & (steps <= s1)
        tt = reduce(vals["t2m"][:, sel], axis=1) if sel.sum() >= 3 else None
    pp = None
    if "tp" in vals:
        tp = vals["tp"]
        j0, j1 = np.nonzero(steps == s0)[0], np.nonzero(steps == s1)[0]
        if j1.size:
            acc0 = tp[:, j0[0]] if j0.size else (np.zeros(tp.shape[0]) if s0 == 0 else None)
            if acc0 is not None:
                pp = np.clip(tp[:, j1[0]] - acc0, 0.0, None)
    wv = None
    if "u10" in vals and "v10" in vals:
        hours = (3, 6, 9, 12) if kind == "day" else (6, 9, 12)
        sel = np.isin(_valid(run), [start + dt.timedelta(hours=h) for h in hours])
        if sel.any():
            wv = np.hypot(vals["u10"], vals["v10"])[:, sel].max(axis=1)
    return tt, pp, wv


def period_rows(run: BoxRun, pts: list[Site], *, lapse: str | float = "std", days: int = 5) -> list[dict]:
    """Model values per site and 白天/夜间 period (:func:`period_stats`), in
    :class:`wxgrid.verify.Matched` row form, for the periods the cycle's issue covers."""
    vals = point_values(run, pts, lapse=lapse)
    issue = run.init + dt.timedelta(hours=ISSUE_DELAY_H)
    rows = []
    for date, kind, start, end in period_windows(run.init, days):
        tt, pp, wv = period_stats(run, vals, start, end, kind)
        if tt is None and pp is None and wv is None:
            continue
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


def ensemble_period_rows(run: BoxRun, pts: list[Site], *, days: int = 5, wet_mm: float = 0.1) -> list[dict]:
    """Per site and period: share of ensemble members with ≥ ``wet_mm`` (``f_pop``, 0–1) and the
    ensemble-mean amount (``f_pens``), from a run whose fields are per-member accumulations
    (``tp_<member>``, see :func:`wxgrid.sources.raw.fetch_gefs`)."""
    lat = np.array([p.lat for p in pts])
    lon = np.array([p.lon for p in pts])
    names = sorted(k for k in run.fields if k.startswith("tp_"))
    acc = np.stack([run.bilinear(lat, lon, run.fields[k]).T for k in names])   # (member, point, step)
    steps = run.steps
    issue = run.init + dt.timedelta(hours=ISSUE_DELAY_H)
    rows = []
    for date, kind, start, end in period_windows(run.init, days):
        s0, s1 = (start - run.init).total_seconds() / 3600, (end - run.init).total_seconds() / 3600
        j1 = np.nonzero(steps == s1)[0]
        j0 = np.nonzero(steps == s0)[0]
        if not j1.size or (s0 > 0 and not j0.size):
            continue
        amt = acc[:, :, j1[0]] - (acc[:, :, j0[0]] if s0 > 0 else 0.0)        # (member, point)
        ok = np.isfinite(amt)
        n = ok.sum(axis=0)
        with np.errstate(invalid="ignore"):
            pop = np.where(n >= 10, (np.where(ok, amt, 0) >= wet_mm).sum(axis=0) / np.maximum(n, 1), np.nan)
            mean = np.where(n >= 10, np.nansum(np.where(ok, amt, 0), axis=0) / np.maximum(n, 1), np.nan)
        lead = lead_day(end, issue)
        for i, p in enumerate(pts):
            rows.append({"model": run.source, "station": p.id, "date": date, "kind": kind, "lead": lead,
                         "init": run.init.isoformat(), "end": end.isoformat(),
                         "f_pop": _num(pop, i), "f_pens": _num(mean, i)})
    return rows


# ------------------------------------------------------------------ live forecast

#: Members of the native consensus (keys of :data:`wxgrid.sources.raw.MODELS`).
MEMBERS = ("ifs", "aifs", "gfs")
#: Degrees around townships and stations kept from each model field.
PAD = 0.75


def choose_cycle(issue: dt.datetime, last_lead, *, sess=None, members=MEMBERS, min_members: int = 2,
                 lookback: int = 4) -> tuple[dt.datetime, list[str]]:
    """Newest 00/12 UTC cycle that at least ``min_members`` members have published deep enough.

    ``last_lead(cycle)`` gives the hours from that cycle to the end of the
    forecast. 00/12 UTC only: IFS 06/18 UTC stops at 90 h. Returns the cycle
    and the members that have it.
    """
    from .sources import raw

    t = issue - dt.timedelta(hours=5)
    cand = dt.datetime(t.year, t.month, t.day, 12 if t.hour >= 12 else 0)
    for k in range(lookback):
        cycle = cand - dt.timedelta(hours=12 * k)
        need = last_lead(cycle)
        have = []
        for m in members:
            model = raw.MODELS[m]
            last = int(np.ceil(need / model.cadence) * model.cadence)
            if last <= model.max_lead[cycle.hour] and raw.probe(model, cycle, last, sess=sess):
                have.append(m)
        if len(have) >= min_members:
            return cycle, have
    raise RuntimeError(f"no 00/12 UTC cycle in the last {lookback * 12} h has {min_members} members out far enough")


def _interp_steps(arr: np.ndarray, src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Linear interpolation along the step axis of ``(point, step)``; NaN outside ``src``."""
    out = np.full((arr.shape[0], dst.size), np.nan)
    for i in range(arr.shape[0]):
        ok = np.isfinite(arr[i])
        if ok.sum() >= 2:
            out[i] = np.interp(dst, src[ok], arr[i][ok], left=np.nan, right=np.nan)
    return out


def member_dataset(run: BoxRun, pts: list[Site], init_v: dt.datetime, steps_v: np.ndarray,
                   is_day: np.ndarray, names: list[str]):
    """One member on the engine's 3-hourly ``(point, step)`` grid, from ``init_v``.

    Temperatures take the standard lapse rate in daytime windows and the
    model's own at night (as in training); 6-hourly members are interpolated in
    time and their 6 h precipitation split evenly.
    """
    import xarray as xr

    off = int(round((init_v - run.init).total_seconds() / 3600))
    hours = off + np.asarray(steps_v, dtype=int)
    std, loc = point_values(run, pts, lapse="std"), point_values(run, pts, lapse="local")
    src = run.steps.astype(float)
    # accumulations need a node at step 0 for the first window
    src0 = np.r_[0.0, src]

    def acc(field):
        a = std[field]
        return _interp_steps(np.column_stack([np.zeros(a.shape[0]), a]), src0, hours.astype(float)), \
            _interp_steps(np.column_stack([np.zeros(a.shape[0]), a]), src0, (hours - 3).astype(float))

    data = {}
    for f in ("t2m", "tmax", "tmin"):
        if f in std:
            mixed = np.where(is_day[None, :], _interp_steps(std[f], src, hours), _interp_steps(loc[f], src, hours))
            data[f] = mixed
    t2m = data["t2m"]
    if "tmax" in data and run.meta.get("cadence", 3) <= 3:
        data["tmax3"], data["tmin3"] = np.fmax(data.pop("tmax"), t2m), np.fmin(data.pop("tmin"), t2m)
    else:
        data.pop("tmax", None), data.pop("tmin", None)
        prev = np.where(is_day[None, :], _interp_steps(std["t2m"], src, hours - 3), _interp_steps(loc["t2m"], src, hours - 3))
        data["tmax3"], data["tmin3"] = np.fmax(prev, t2m), np.fmin(prev, t2m)
    for f in ("tp", "snow"):
        if f in std:
            a1, a0 = acc(f)
            data["precip" if f == "tp" else "snow"] = np.clip(a1 - a0, 0.0, None)
    for f, name in (("u10", "u10"), ("v10", "v10"), ("gust", "gust"), ("tcc", "cloud")):
        if f in std:
            data[name] = _interp_steps(std[f], src, hours)
    if "cloud" in data:
        data["cloud"] = np.clip(data["cloud"], 0.0, 1.0)
    lat = np.array([p.lat for p in pts])
    lon = np.array([p.lon for p in pts])
    zmod = run.bilinear(lat, lon, run.orog) if run.orog is not None else np.full(lat.size, np.nan)
    coords = {"point": [p.id for p in pts], "step": np.asarray(steps_v, dtype=int),
              "valid_time": ("step", np.datetime64(init_v, "s") + np.asarray(steps_v).astype("timedelta64[h]")),
              "latitude": ("point", lat), "longitude": ("point", lon),
              "elevation": ("point", [p.elevation_m for p in pts]), "model_elevation": ("point", zmod),
              "name": ("point", names)}
    return xr.Dataset({k: (("point", "step"), v) for k, v in data.items()}, coords=coords,
                      attrs={"init_time": init_v.isoformat(), "source": run.source, "cycle": run.stamp})


def _window_keys(plan, init_v: dt.datetime, steps_v: np.ndarray, cycle: dt.datetime, tz: float):
    """``(kind, lead, period index | None)`` for each 3 h window ``(step-3, step]``."""
    issue_nominal = cycle + dt.timedelta(hours=ISSUE_DELAY_H)
    out = []
    for s in steps_v:
        s = int(s)
        k = next((i for i, p in enumerate(plan) if p.start_lead <= s - 3 and s <= p.end_lead), None)
        if k is not None:
            p = plan[k]
            end = init_v + dt.timedelta(hours=p.end_lead)
            out.append((p.kind, lead_day(end, issue_nominal), k))
        else:
            h0 = (init_v + dt.timedelta(hours=s - 3 + tz)).hour
            end = init_v + dt.timedelta(hours=s)
            out.append(("day" if 8 <= h0 < 20 else "night", lead_day(end, issue_nominal), None))
    return out


def _period_bounds(p, init_v: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    return init_v + dt.timedelta(hours=p.start_lead), init_v + dt.timedelta(hours=p.end_lead)


def compute(points, *, county: str, seat: str, days: int = 5, tz: float = 8.0, want_pop: bool = True,
            workers: int = 8, issue_utc=None, sess=None, hourly: bool = False, calibrate: bool = True,
            data_dir=None, stations=None, members=MEMBERS):
    """The product (and optionally the hourly series) from raw IFS + AIFS + GFS (+ GEFS).

    Every member run is archived for the calibration; the consensus and its
    corrections are in :mod:`wxgrid.consensus`.
    """
    import pathlib

    import xarray as xr

    from . import archive, bulletin, consensus
    from . import hourly as hourly_mod
    from . import obs as obs_mod
    from . import periods as periods_mod
    from . import product
    from .gribbox import Box
    from .sources import gefs as gefs_mod
    from .sources import raw
    from .sources._fetch import session

    sess = sess or session()
    data = pathlib.Path(data_dir or "/var/lib/wxgrid")
    arch_root, cal_root, obs_root = data / "archive", data / "native", data / "verify" / "obs"
    stations = list(stations if stations is not None else obs_mod.NEAR_YANSHAN)
    issue = issue_utc or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    issue = issue.replace(tzinfo=None) if isinstance(issue, dt.datetime) else dt.datetime.fromisoformat(str(issue))
    init_v = product.virtual_init(issue)

    def plan_for(cycle):
        off = int((init_v - cycle).total_seconds() // 3600)
        return periods_mod.plan(init_v, issue, tz=tz, n_days=days, max_lead=raw.IFS.max_lead[0] - off)

    cycle, have = choose_cycle(issue, lambda c: int((init_v - c).total_seconds() // 3600) + plan_for(c)[-1].end_lead,
                               sess=sess, members=members)
    plan = plan_for(cycle)
    steps_v = np.array(periods_mod.steps_for(plan))
    last = int((init_v - cycle).total_seconds() // 3600) + int(steps_v[-1])
    box = Box.around([*points, *stations], pad=PAD)

    runs = {}
    for m in have:
        model = raw.MODELS[m]
        try:
            runs[m] = raw.fetch(model, cycle, model.steps(cycle.hour, int(np.ceil(last / model.cadence) * model.cadence)),
                                box, sess=sess, workers=workers)
            archive.store(arch_root, runs[m])
        except Exception as exc:  # noqa: BLE001 — a member short is a weaker forecast, not none
            print(f"[native] {m} {cycle:%Y%m%d%H} failed: {type(exc).__name__}: {exc}", flush=True)
    if len(runs) < 2:
        raise RuntimeError(f"native engine: only {sorted(runs)} of {members} could be read for {cycle:%Y%m%d%H}")
    ens_run = None
    if want_pop:
        try:
            ens_run = raw.fetch_gefs(cycle, range(6, int(np.ceil(last / 6) * 6) + 1, 6), box, sess=sess,
                                     workers=max(workers, 16))
            archive.store(arch_root, ens_run)
        except Exception as exc:  # noqa: BLE001 — probabilities are detail only
            print(f"[native] gefs {cycle:%Y%m%d%H} failed: {type(exc).__name__}: {exc}", flush=True)
    archive.prune(arch_root)
    cal = consensus.refresh(cal_root, archive_root=arch_root, obs_root=obs_root, stations=stations,
                            sess=sess) if calibrate else None

    pts = sites(points)
    names = [p.name for p in points]
    keys = _window_keys(plan, init_v, steps_v, cycle, tz)
    is_day = np.array([k[0] == "day" for k in keys])
    mds = {m: member_dataset(r, pts, init_v, steps_v, is_day, names) for m, r in runs.items()}

    # ---- temperatures: weighted consensus + bias, per window's (kind, lead)
    temp_cal = (cal or {}).get("temp")
    out = {}
    for f in ("t2m", "tmax3", "tmin3"):
        arr = np.full((len(pts), steps_v.size), np.nan)
        for (kind, lead) in {(k, ld) for k, ld, _ in keys}:
            sel = np.array([k == kind and ld == lead for k, ld, _ in keys])
            arr[:, sel] = consensus.combine_temperature(temp_cal, kind, lead,
                                                        {m: ds[f].values[:, sel] for m, ds in mds.items()})
        out[f] = arr
    out["tmax3"], out["tmin3"] = np.fmax(out["tmax3"], out["t2m"]), np.fmin(out["tmin3"], out["t2m"])

    def mean_of(f):
        stack = [ds[f].values for ds in mds.values() if f in ds]
        if not stack:
            return None
        with np.errstate(invalid="ignore"):
            return np.nanmean(np.stack(stack), axis=0)

    # ---- precipitation: member mean, 12 h totals quantile-mapped, windows scaled
    precip = mean_of("precip")
    snow = mean_of("snow")
    if snow is None:
        snow = np.zeros_like(precip)
    for k, p in enumerate(plan):
        sel = np.array([key[2] == k for key in keys])
        if not sel.any():
            continue
        tot = np.nansum(precip[:, sel], axis=1)
        mapped = consensus.map_precip((cal or {}).get("precip"), p.kind, tot)
        fac = np.where(tot > 1e-6, mapped / np.where(tot > 1e-6, tot, 1.0), 0.0)
        precip[:, sel] *= fac[:, None]
        snow[:, sel] *= fac[:, None]
    out["precip"], out["snow"] = precip, np.minimum(snow, precip)
    u, v = mean_of("u10"), mean_of("v10")
    out["u10"], out["v10"] = u, v
    speed = np.hypot(u, v)
    gust = mean_of("gust")
    out["gust"] = np.fmax(gust, speed) if gust is not None else speed
    cloud = mean_of("cloud")
    out["cloud"] = cloud if cloud is not None else np.where(precip >= 0.1, 0.9, 0.3)
    out["wind_speed"] = speed
    out["wind_dir"] = (270.0 - np.degrees(np.arctan2(v, u))) % 360.0
    first = next(iter(mds.values()))
    ds = xr.Dataset({k: (("point", "step"), a) for k, a in out.items()}, coords=first.coords,
                    attrs={"init_time": init_v.isoformat(),
                           "source": "native(" + "+".join(runs) + f" {cycle:%Y%m%d%H})"})

    # ---- period extremes: consensus of each member's own period max/min (what was trained)
    issue_nominal = cycle + dt.timedelta(hours=ISSUE_DELAY_H)
    ext = {"tmax": np.full((len(pts), len(plan)), np.nan), "tmin": np.full((len(pts), len(plan)), np.nan)}
    vals = {m: {"std": point_values(r, pts, lapse="std"), "local": point_values(r, pts, lapse="local")}
            for m, r in runs.items()}
    member_tot = {m: np.full((len(pts), len(plan)), np.nan) for m in runs}
    for k, p in enumerate(plan):
        start, end = _period_bounds(p, init_v)
        per = {}
        for m, r in runs.items():
            tt, pp, _ = period_stats(r, vals[m]["std" if p.kind == "day" else "local"], start, end, p.kind)
            per[m] = tt if tt is not None else np.full(len(pts), np.nan)
            if pp is not None:
                member_tot[m][:, k] = pp
        t = consensus.combine_temperature(temp_cal, p.kind, lead_day(end, issue_nominal), per)
        ext["tmax" if p.kind == "day" else "tmin"][:, k] = t

    # ---- probabilities (detail only)
    pp = sp = None
    n_ens = 0
    if ens_run is not None:
        names_m = sorted(f for f in ens_run.fields if f.startswith("tp_"))
        lat = np.array([q.lat for q in pts])
        lon = np.array([q.lon for q in pts])
        accm = np.stack([ens_run.bilinear(lat, lon, ens_run.fields[f]).T for f in names_m])  # (member, point, step)
        good = np.isfinite(accm).all(axis=(1, 2))
        accm = accm[good]
        n_ens = int(good.sum())
        if n_ens >= 10:
            buckets = np.diff(np.concatenate([np.zeros(accm.shape[:2] + (1,)), accm], axis=2), axis=2).clip(min=0)
            ens = gefs_mod.EnsemblePrecip(mm=buckets, starts_h=ens_run.steps - 6, ends_h=ens_run.steps,
                                          members=tuple(np.array(names_m)[good]),
                                          init_time=np.datetime64(cycle, "s"), point_ids=tuple(q.id for q in pts))
            sp = hourly_mod.hourly_pop(ens, np.datetime64(init_v, "s"), steps_v)
            pp = np.full((len(pts), len(plan)), np.nan)
            for k, p in enumerate(plan):
                start, end = _period_bounds(p, init_v)
                s0, s1 = (start - cycle).total_seconds() / 3600, (end - cycle).total_seconds() / 3600
                j0, j1 = np.nonzero(ens_run.steps == s0)[0], np.nonzero(ens_run.steps == s1)[0]
                if not j1.size or (s0 > 0 and not j0.size):
                    continue
                amt = accm[:, :, j1[0]] - (accm[:, :, j0[0]] if s0 > 0 else 0.0)
                share = (amt >= consensus.WET_MM).mean(axis=0)
                pp[:, k] = 100.0 * consensus.predict_pop((cal or {}).get("pop"), p.kind, share,
                                                         {m: member_tot[m][:, k] for m in runs})

    prod = product.build(ds, periods=plan, issue_utc=issue, county=county, seat=seat,
                         run=init_v.strftime("%Y%m%d%H"), member="native", sources=tuple(runs), weights=None,
                         tz=tz, extremes=ext, period_pop=pp, step_pop=sp, pop_members=n_ens)
    meta = prod["meta"]
    meta["engine"] = "native"
    meta["cycle"] = cycle.strftime("%Y%m%d%H")
    meta["member_names"] = [raw.MODELS[m].label for m in runs]
    meta["pop_source"] = "native" if pp is not None else None
    meta["pop_run"] = cycle.strftime("%Y%m%d%H") if pp is not None else None
    meta["calibration"] = None if not cal else {
        k: cal.get(k) for k in ("method", "window", "n_pairs", "generated", "lapse")}
    if cal:
        meta["weights"] = {kind: {lead: e["w"] for lead, e in by.items()} for kind, by in cal["temp"].items()}
    meta["attribution"] = "ECMWF open data (CC BY 4.0); NOAA GFS/GEFS (public domain)"
    prod["verification"] = consensus.load_scores(cal_root)
    prod["text"] = bulletin.render(prod)
    if not hourly:
        return prod, None
    hr = hourly_mod.disaggregate(ds, None)
    hpop = None
    if sp is not None:
        col = {int(s): j for j, s in enumerate(steps_v)}
        idx = [col.get(int(-(-h // 3) * 3)) for h in hr["step"].values]
        hpop = np.stack([sp[:, j] if j is not None else np.full(sp.shape[0], np.nan) for j in idx], axis=1)
    hdoc = hourly_mod.build(hr, county=county, seat=seat, run=init_v.strftime("%Y%m%d%H"), tz=tz, pop=hpop,
                            pop_members=n_ens, pop_run=meta["pop_run"])
    hdoc["meta"]["source"] = "native"
    return prod, hdoc
