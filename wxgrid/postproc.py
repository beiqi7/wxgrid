"""Station-trained post-processing for the multi-model forecast.

Two corrections, both learned from the national SYNOP stations around the county
(:mod:`wxgrid.obs`) against what the same members predicted there before
(Open-Meteo's previous-runs archive, :mod:`wxgrid.verify`):

* **Temperature bias** — a decaying-average bias of the member mean, per period
  kind (白天最高 / 夜间最低) and lead day, pooled over the lowland stations::

      b <- (1 - α) b + α · mean_station(forecast - observed)        α = 0.1

  Pooled because a single station is noisy and the townships are not stations;
  a leave-one-station-out test showed the pooled bias transfers (MAE 0.95 ->
  0.83 ℃ at day 1 on the station left out).

* **Precipitation quantile mapping** — the member mean rains lightly far too
  often (frequency bias ~3: drizzle from averaging) and too little in heavy
  events. Each 12 h period total is mapped through the forecast and observed
  climatologies of the training window, ``P' = F_obs⁻¹(F_fc(P))``; the period's
  3-hourly amounts are scaled by ``P'/P`` so timing is kept and totals agree.

Scores are always computed out of sample: bias online without look-ahead, the
quantile map fitted on the 30 days before the 30 scored (see :func:`evaluate`).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
from collections import defaultdict
from typing import Any

import numpy as np
import xarray as xr

from . import obs as obs_mod
from . import verify
from .sources import openmeteo
from .sources._fetch import session

DEFAULT_ROOT = pathlib.Path(os.environ.get("WXGRID_VERIFY_DIR", "/var/lib/wxgrid/verify"))
ARCHIVE_DAYS = 62
TRAIN_DAYS = 60
ALPHA = 0.1
MAX_BIAS_K = 4.0
QMAP_RATIO = (0.5, 3.0)       # tail ratio clamp for amounts above the training range
#: Never raise a forecast amount by more than this factor. The upper tail of a
#: 60-day window rests on a handful of convective cases; uncapped, 16 mm in 12 h
#: became 32 mm (暴雨). Verified with the cap in place.
MAX_UP = 1.5
LOWLAND_M = 500.0             # 庐山 (1165 m) is a summit, not like any township
#: Reference configurations for the page's accuracy table.
REFERENCE = {"ecmwf": {"ecmwf_ifs": 1.0}, "old": {"ecmwf_ifs025": 0.6, "gfs_global": 0.4}}


# ------------------------------------------------------------------ archive

def _prev_path(root: pathlib.Path, model: str) -> pathlib.Path:
    return root / "previous" / f"{model}.json"


def _load_prev(root, model) -> dict[str, dict[str, dict[str, float]]]:
    """``{station: {time: {key: value}}}``."""
    p = _prev_path(root, model)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _as_recs(store: dict) -> list[dict]:
    """Archive form -> the Open-Meteo-like records :func:`verify.model_periods` reads."""
    recs = []
    for wmo, by_t in store.items():
        times = sorted(by_t)
        keys = sorted({k for v in by_t.values() for k in v})
        recs.append({"station": wmo, "time": times, **{k: [by_t[t].get(k) for t in times] for k in keys}})
    return recs


def update_archive(root: pathlib.Path = DEFAULT_ROOT, *, stations=obs_mod.NEAR_YANSHAN,
                   models=None, sess=None, today: dt.date | None = None) -> dict[str, Any]:
    """Bring observations and every model's previous-run archive up to yesterday."""
    root = pathlib.Path(root)
    sess = sess or session()
    today = today or dt.datetime.now(dt.timezone.utc).date()
    models = list(models or [*openmeteo.MEMBERS, *{m for r in REFERENCE.values() for m in r}])
    report: dict[str, Any] = {"obs": obs_mod.update(obs_mod.ObsStore(root / "obs"), stations,
                                                    days=ARCHIVE_DAYS, sess=sess)}
    stations = list(stations)
    end = today - dt.timedelta(days=1)
    oldest = (today - dt.timedelta(days=ARCHIVE_DAYS)).isoformat()
    for m in models:
        store = _load_prev(root, m)
        last = max((max(v) for v in store.values() if v), default=None)
        start = today - dt.timedelta(days=ARCHIVE_DAYS) if last is None else \
            max(today - dt.timedelta(days=ARCHIVE_DAYS), dt.date.fromisoformat(last[:10]) - dt.timedelta(days=3))
        if start > end:
            report[m] = 0
            continue
        try:
            recs = verify.fetch_model(m, stations, start, end, cache_dir=root / "fetch", sess=sess, pause=1.0)
        except Exception as exc:  # noqa: BLE001 — one model down must not stop calibration
            report[m] = f"failed: {type(exc).__name__}"
            continue
        for r in recs:
            by_t = store.setdefault(r["station"], {})
            keys = [k for k in r if k not in ("station", "time")]
            for i, t in enumerate(r["time"]):
                vals = {k: r[k][i] for k in keys if r[k][i] is not None}
                if vals:
                    by_t.setdefault(t, {}).update(vals)
        for wmo in store:
            store[wmo] = {t: v for t, v in store[wmo].items() if t >= oldest}
        p = _prev_path(root, m)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.with_suffix(".part").write_text(json.dumps(store), encoding="utf-8")
        p.with_suffix(".part").replace(p)
        report[m] = sum(len(v) for v in store.values())
        for f in (root / "fetch").glob(f"{m}_*.json"):  # one-shot download cache
            f.unlink(missing_ok=True)
    return report


# ------------------------------------------------------------------ matched rows

def _mean_rows(by_model: dict[str, list[dict]], members: dict[str, float], name: str,
               min_members: int) -> list[dict]:
    g: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for m in members:
        for r in by_model.get(m, []):
            g[(r["station"], r["date"], r["kind"], r["lead"])][m] = r
    out = []
    for key, rs in g.items():
        a = next(iter(rs.values()))
        row = {"model": name, "station": key[0], "date": key[1], "kind": key[2], "lead": key[3],
               "o_t": a["o_t"], "o_p": a["o_p"], "o_w": a["o_w"]}
        for f in ("f_t", "f_p", "f_w"):
            v = [(x[f], members[m]) for m, x in rs.items() if x[f] is not None]
            row[f] = sum(a * w for a, w in v) / sum(w for _, w in v) if len(v) >= min_members else None
        out.append(row)
    return out


def matched(root: pathlib.Path = DEFAULT_ROOT, *, stations=obs_mod.NEAR_YANSHAN,
            today: dt.date | None = None, days: int = TRAIN_DAYS) -> dict[str, list[dict]]:
    """``{config: rows}`` for the member mean and the reference configurations."""
    root = pathlib.Path(root)
    today = today or dt.datetime.now(dt.timezone.utc).date()
    dates = [today - dt.timedelta(days=i) for i in range(days, 0, -1)]
    low = [s for s in stations if s.elevation_m < LOWLAND_M]
    store = obs_mod.ObsStore(root / "obs")
    observed = {s.wmo: store.load(s.wmo) for s in low}
    wanted = {*openmeteo.MEMBERS, *{m for r in REFERENCE.values() for m in r}}
    fc = {m: [r for r in _as_recs(_load_prev(root, m)) if r["station"] in observed] for m in wanted}
    M = verify.Matched.build(fc, observed, dates)
    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in M.rows:
        by_model[r["model"]].append(r)
    out = {"new_raw": _mean_rows(by_model, {m: 1.0 for m in openmeteo.MEMBERS}, "new_raw",
                                 openmeteo.MIN_MEMBERS)}
    for name, mem in REFERENCE.items():
        out[name] = _mean_rows(by_model, mem, name, len(mem))
    out["members"] = {m: by_model.get(m, []) for m in openmeteo.MEMBERS}
    return out


# ------------------------------------------------------------------ fitting

def _bias_state(rows: list[dict], alpha: float) -> dict[str, dict[int, float]]:
    """Latest decaying-average bias per (kind, lead), pooled over stations."""
    out: dict[str, dict[int, float]] = {"day": {}, "night": {}}
    by: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r["f_t"] is not None and r["o_t"] is not None:
            by[(r["kind"], r["lead"])][r["date"]].append(r["f_t"] - r["o_t"])
    for (kind, lead), per_date in by.items():
        b = None
        for d in sorted(per_date):
            e = float(np.mean(per_date[d]))
            b = e if b is None else (1 - alpha) * b + alpha * e
        if b is not None:
            out[kind][int(lead)] = float(np.clip(b, -MAX_BIAS_K, MAX_BIAS_K))
    return out


def _qmap_fit(rows: list[dict], n: int = 401) -> dict[str, list[float]] | None:
    f, o = verify.Matched.arrays(rows, "f_p", "o_p")
    if f.size < 60:
        return None
    p = np.linspace(0.0, 1.0, n)
    return {"p": p.round(5).tolist(), "fq": np.quantile(f, p).round(3).tolist(),
            "oq": np.quantile(o, p).round(3).tolist(), "n": int(f.size)}


def qmap(x, fit: dict[str, list[float]] | None):
    """Map forecast amounts through the fitted climatologies (vectorised)."""
    x = np.asarray(x, dtype=float)
    if fit is None:
        return x
    p, fq, oq = (np.asarray(fit[k], dtype=float) for k in ("p", "fq", "oq"))
    ramp = fq + p * 1e-9                    # ties (many zeros) -> strictly increasing
    prob = np.interp(x, ramp, p)
    out = np.interp(prob, p, oq)
    top = fq[-1]
    if top > 0:
        ratio = float(np.clip(oq[-1] / top, *QMAP_RATIO))
        out = np.where(x > top, x * ratio, out)
    out = np.minimum(out, x * MAX_UP)
    return np.where(np.isfinite(x), np.maximum(out, 0.0), np.nan)


def _member_qmaps(members: dict[str, list[dict]], *, before: str | None = None) -> dict[str, dict]:
    return {m: {k: _qmap_fit([r for r in rows if r["kind"] == k and (before is None or r["date"] < before)])
                for k in ("day", "night")} for m, rows in members.items()}


def member_pop(rows_by_member: dict[str, list[dict]], fits: dict[str, dict]) -> dict[tuple, float]:
    """Share of members with >= 0.1 mm after each member's own quantile map, per (station, date, kind, lead)."""
    wet: dict[tuple, list[float]] = defaultdict(list)
    for m, rows in rows_by_member.items():
        for r in rows:
            if r["f_p"] is None:
                continue
            v = float(qmap(r["f_p"], (fits.get(m) or {}).get(r["kind"])))
            wet[(r["station"], r["date"], r["kind"], r["lead"])].append(1.0 if v >= verify.WET_MM else 0.0)
    return {k: float(np.mean(v)) for k, v in wet.items() if len(v) >= openmeteo.MIN_MEMBERS}


def fit(rows: list[dict], *, alpha: float = ALPHA, members: dict[str, list[dict]] | None = None) -> dict[str, Any]:
    """Calibration from the member-mean rows (all dates are in the past)."""
    dates = sorted({r["date"] for r in rows})
    return {
        "method": "decaying-average temperature bias (pooled, per kind and lead) + 12 h precipitation quantile map",
        "alpha": alpha, "window": [dates[0], dates[-1]] if dates else None,
        "n_pairs": len(rows), "members": list(openmeteo.MEMBERS),
        "bias": _bias_state(rows, alpha),
        "qmap": {k: _qmap_fit([r for r in rows if r["kind"] == k]) for k in ("day", "night")},
        "qmap_members": _member_qmaps(members) if members else {},
    }


# ------------------------------------------------------------------ evaluation

def _scores(rows: list[dict]) -> dict[int, dict]:
    out = {}
    for lead in verify.LEADS:
        rs = [r for r in rows if r["lead"] == lead]
        fp, op = verify.Matched.arrays(rs, "f_p", "o_p")
        out[lead] = {
            "tmax": verify.temp_scores(*verify.Matched.arrays([r for r in rs if r["kind"] == "day"], "f_t", "o_t")),
            "tmin": verify.temp_scores(*verify.Matched.arrays([r for r in rs if r["kind"] == "night"], "f_t", "o_t")),
            "rain": verify.rain_scores(fp, op),
            "rain5": verify.rain_scores(fp, op, f_thr=verify.MODERATE_12H_MM, o_thr=verify.MODERATE_12H_MM),
        }
    return out


def evaluate(configs: dict[str, list[dict]], *, test_days: int = 30, alpha: float = ALPHA) -> dict[str, Any]:
    """Out-of-sample scores over the last ``test_days``: the new system and its references."""
    all_dates = sorted({r["date"] for r in configs["new_raw"]})
    if len(all_dates) < test_days + 10:
        return {"error": "not enough verified days yet", "days": len(all_dates)}
    cut = all_dates[-test_days]
    raw = configs["new_raw"]
    corr = verify.online_bias(raw, alpha=alpha, pooled=True)
    fits = {k: _qmap_fit([r for r in raw if r["date"] < cut and r["kind"] == k]) for k in ("day", "night")}
    for r in corr:
        if r["f_p"] is not None:
            r["f_p"] = float(qmap(r["f_p"], fits[r["kind"]]))
    test = lambda rows: [r for r in rows if r["date"] >= cut]  # noqa: E731
    stations = sorted({r["station"] for r in raw})
    pop_scores = None
    if configs.get("members"):
        pop = member_pop({m: test(rs) for m, rs in configs["members"].items()},
                         _member_qmaps(configs["members"], before=cut))
        obs_wet = {(r["station"], r["date"], r["kind"], r["lead"]): r["o_p"] >= verify.WET_MM
                   for r in test(raw) if r["o_p"] is not None}
        keys = [k for k in pop if k in obs_wet]
        if keys:
            f = np.array([pop[k] for k in keys])
            o = np.array([float(obs_wet[k]) for k in keys])
            base = float(np.mean([float(r["o_p"] >= verify.WET_MM) for r in raw
                                  if r["date"] < cut and r["o_p"] is not None]))
            bs, bs_clim = float(((f - o) ** 2).mean()), float(((base - o) ** 2).mean())
            bins = [(0.0, 0.2), (0.2, 0.5), (0.5, 0.8), (0.8, 1.01)]
            pop_scores = {"n": len(keys), "brier": bs, "brier_climatology": bs_clim,
                          "bss": 1 - bs / bs_clim if bs_clim > 0 else None,
                          "reliability": [{"bin": [a, min(b, 1.0)], "n": int(((f >= a) & (f < b)).sum()),
                                           "observed": float(o[(f >= a) & (f < b)].mean()) if ((f >= a) & (f < b)).any() else None}
                                          for a, b in bins]}
    return {
        "window": [cut, all_dates[-1]], "days": test_days, "stations": stations,
        "configs": {
            "new": {"label": "本系统：8 家模式 + 站点订正", "scores": _scores(test(corr))},
            "new_raw": {"label": "8 家模式平均（未订正）", "scores": _scores(test(raw))},
            "old": {"label": "原方案：ECMWF 0.6 + GFS 0.4", "scores": _scores(test(configs["old"]))},
            "ecmwf": {"label": "单一 ECMWF IFS 9 km", "scores": _scores(test(configs["ecmwf"]))},
        },
        "pop": pop_scores,
        "notes": "白天最高对比 12 时（UTC）报的 24 小时最高气温，夜间最低对比 00 时报的 24 小时最低气温；"
                 "降水为 12 小时（白天 08—20 时、夜间 20—08 时），≥0.1 mm 为有雨，微量按无雨。"
                 "订正全部样本外：偏差只用预报发出前的误差，分位数映射用评分窗口之前 30 天拟合。",
    }


def refresh(root: pathlib.Path = DEFAULT_ROOT, *, stations=obs_mod.NEAR_YANSHAN, sess=None,
            today: dt.date | None = None, max_age_days: float = 1.0) -> dict[str, Any] | None:
    """Update the archive, refit and rescore at most once per ``max_age_days``.

    Returns the calibration in force (possibly the previous one when the refresh
    failed), or None when there has never been one.
    """
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    cal_path = root / "calibration.json"
    cur = json.loads(cal_path.read_text(encoding="utf-8")) if cal_path.exists() else None
    if cur and cur.get("generated"):
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(cur["generated"])
        if age < dt.timedelta(days=max_age_days):
            return cur
    try:
        update_archive(root, stations=stations, sess=sess, today=today)
        configs = matched(root, stations=stations, today=today)
        cal = fit(configs["new_raw"], members=configs.get("members"))
        if not cal["bias"]["day"] and not cal["bias"]["night"]:
            raise RuntimeError("no matched forecast/observation pairs")
        cal["generated"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        scores = evaluate(configs)
        scores["generated"] = cal["generated"]
        for path, obj in ((cal_path, cal), (root / "scores.json", scores)):
            path.with_suffix(".part").write_text(json.dumps(obj, ensure_ascii=False, default=float), encoding="utf-8")
            path.with_suffix(".part").replace(path)
        return cal
    except Exception as exc:  # noqa: BLE001 — keep forecasting with the last good calibration
        import sys
        print(f"[postproc] calibration refresh failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        if cur:
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(cur["generated"])
            if age < dt.timedelta(days=7):
                return cur
        return None


def load_scores(root: pathlib.Path = DEFAULT_ROOT) -> dict[str, Any] | None:
    p = pathlib.Path(root) / "scores.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (ValueError, OSError):
        return None


# ------------------------------------------------------------------ applying

def lead_day(end_lead_from_issue_h: float) -> int:
    """Verification lead day (1-5) for a period ending this many hours after issue."""
    return int(min(5, max(1, round(end_lead_from_issue_h / 24.0))))


def _window_keys(steps: np.ndarray, gap: int, periods, init_h: int, issue_h: float, tz: float):
    """(kind, lead day, period index | None) for each window ``(step-gap, step]``."""
    off = int(round(tz))
    keys = []
    for s in steps:
        s = int(s)
        k = next((i for i, p in enumerate(periods) if p.start_lead <= s - gap and s <= p.end_lead), None)
        if k is not None:
            p = periods[k]
            keys.append((p.kind, lead_day(init_h + p.end_lead - issue_h), k))
        else:
            h0 = (init_h + s - gap + off) % 24
            kind = "day" if 8 <= h0 < 20 else "night"
            keys.append((kind, 1 if periods and s <= periods[0].start_lead else 5, None))
    return keys


def apply(ds: xr.Dataset, periods, cal: dict[str, Any] | None, *, issue_utc, tz: float = 8.0
          ) -> tuple[xr.Dataset, dict[int, np.ndarray]]:
    """Corrected copy of a (point, step) dataset and the per-period precipitation factors."""
    if not cal:
        return ds, {}
    out = ds.copy(deep=True)
    steps = out["step"].values.astype(int)
    gap = int(np.median(np.diff(steps))) if steps.size > 1 else 3
    init_h = int(np.datetime64(str(ds.attrs["init_time"]), "h").astype("int64"))
    issue_h = float(np.datetime64(issue_utc, "s").astype("int64")) / 3600.0
    keys = _window_keys(steps, gap, periods, init_h, issue_h, tz)
    bias = cal.get("bias", {})
    b = np.array([bias.get(kind, {}).get(str(lead), bias.get(kind, {}).get(lead, 0.0)) for kind, lead, _ in keys])
    for var in ("t2m", "tmax3", "tmin3"):
        if var in out:
            out[var] = out[var] - xr.DataArray(b, dims="step", coords={"step": out["step"]})

    factors: dict[int, np.ndarray] = {}
    pr = out["precip"].transpose("point", "step").values.astype(float)
    sn = out["snow"].transpose("point", "step").values.astype(float) if "snow" in out else None
    for k, p in enumerate(periods):
        sel = np.array([key[2] == k for key in keys])
        if not sel.any():
            continue
        tot = np.nansum(pr[:, sel], axis=1)
        mapped = qmap(tot, (cal.get("qmap") or {}).get(p.kind))
        fac = np.where(tot > 1e-6, mapped / np.where(tot > 1e-6, tot, 1.0), 0.0)
        factors[k] = fac
        pr[:, sel] *= fac[:, None]
        if sn is not None:
            sn[:, sel] *= fac[:, None]
    out["precip"] = (("point", "step"), pr)
    if sn is not None:
        out["snow"] = (("point", "step"), sn)
    out.attrs["calibration"] = f"{cal.get('window')} alpha={cal.get('alpha')}"
    return out, factors


def member_period_pop(members: dict[str, xr.Dataset], periods, cal: dict[str, Any] | None
                      ) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Detail probabilities from the members themselves, after their own quantile maps.

    Returns ``(period_pop, step_pop)``: ``(point, period)`` and ``(point, step)``
    percentages of members with >= 0.1 mm — per 白天/夜间 period, and per 3-hour
    window (each member's windows scaled by that member's period factor). None
    without a calibration that has member maps.
    """
    fits = (cal or {}).get("qmap_members") or {}
    if not fits or not members:
        return None, None
    first = next(iter(members.values()))
    steps = first["step"].values.astype(int)
    gap = int(np.median(np.diff(steps))) if steps.size > 1 else 3
    n_pt = first.sizes["point"]
    wet_p = np.zeros((n_pt, len(periods)))
    cnt_p = np.zeros((n_pt, len(periods)))
    wet_s = np.zeros((n_pt, steps.size))
    cnt_s = np.zeros((n_pt, steps.size))
    for name, ds in members.items():
        f = fits.get(name)
        if not f:
            continue
        pr = ds["precip"].transpose("point", "step").values.astype(float)
        for k, p in enumerate(periods):
            sel = (steps - gap >= p.start_lead) & (steps <= p.end_lead)
            if sel.sum() != 12 // gap:
                continue
            block = pr[:, sel]
            ok = np.isfinite(block).all(axis=1)
            tot = np.where(ok, block.sum(axis=1), np.nan)
            mapped = qmap(tot, f.get(p.kind))
            fac = np.where(tot > 1e-6, mapped / np.where(tot > 1e-6, tot, 1.0), 0.0)
            wet_p[:, k] += np.where(ok, mapped >= verify.WET_MM, 0)
            cnt_p[:, k] += ok
            win = block * fac[:, None]
            wet_s[:, sel] += np.where(ok[:, None], win >= verify.WET_MM, 0)
            cnt_s[:, sel] += ok[:, None]
    with np.errstate(invalid="ignore", divide="ignore"):
        pp = np.where(cnt_p >= openmeteo.MIN_MEMBERS, wet_p / cnt_p * 100.0, np.nan)
        sp = np.where(cnt_s >= openmeteo.MIN_MEMBERS, wet_s / cnt_s * 100.0, np.nan)
    return pp, sp


def apply_extremes(ext: dict[str, np.ndarray], periods, cal: dict[str, Any] | None, *, init_utc,
                   issue_utc) -> dict[str, np.ndarray]:
    """Bias-correct (point, period) extremes with each period's own kind and lead day."""
    if not cal:
        return ext
    init_h = float(np.datetime64(init_utc, "s").astype("int64")) / 3600.0
    issue_h = float(np.datetime64(issue_utc, "s").astype("int64")) / 3600.0
    bias = cal.get("bias", {})
    out = {k: np.array(v, dtype=float, copy=True) for k, v in ext.items()}
    for k, p in enumerate(periods):
        lead = lead_day(init_h + p.end_lead - issue_h)
        b = bias.get(p.kind, {}).get(str(lead), bias.get(p.kind, {}).get(lead, 0.0))
        for var in out:
            out[var][:, k] -= b
    return out


def apply_hourly(hr: xr.Dataset, periods, cal: dict[str, Any] | None, factors: dict[int, np.ndarray], *,
                 issue_utc, tz: float = 8.0) -> xr.Dataset:
    """Same corrections on an hourly (point, step) dataset, so hourly and 3-hourly agree."""
    if not cal:
        return hr
    out = hr.copy(deep=True)
    steps = out["step"].values.astype(int)
    init_h = int(np.datetime64(str(hr.attrs["init_time"]), "h").astype("int64"))
    issue_h = float(np.datetime64(issue_utc, "s").astype("int64")) / 3600.0
    keys = _window_keys(steps, 1, periods, init_h, issue_h, tz)
    bias = cal.get("bias", {})
    b = np.array([bias.get(kind, {}).get(str(lead), bias.get(kind, {}).get(lead, 0.0)) for kind, lead, _ in keys])
    out["t2m"] = out["t2m"] - xr.DataArray(b, dims="step", coords={"step": out["step"]})
    pr = out["precip"].transpose("point", "step").values.astype(float)
    sn = out["snow"].transpose("point", "step").values.astype(float) if "snow" in out else None
    for j, (_, _, k) in enumerate(keys):
        if k is not None and k in factors:
            pr[:, j] *= factors[k]
            if sn is not None:
                sn[:, j] *= factors[k]
    out["precip"] = (("point", "step"), pr)
    if sn is not None:
        out["snow"] = (("point", "step"), sn)
    return out
