"""Station-trained consensus of the native members.

Trained on the run archive (:mod:`wxgrid.archive`) against the national SYNOP
stations around the county (:mod:`wxgrid.obs`), pooled over the lowland ones,
and refitted daily. Method chosen by a 2025 backtest (Mar–Aug, see README):

* **Temperature** — per period kind (白天最高 / 夜间最低) and lead day::

      T = Σ_m w_m · f_m + a,       w_m ≥ 0,  Σ w_m = 1

  ridge least squares toward equal weights over the last 60 days, recent days
  weighted more (20-day half-life, the bias drifts with the season), ``a`` the
  weighted mean residual. Weights summing to one keep the elevation signal between
  townships intact (a free regression damps toward the station average). The
  members' values are brought to the point's elevation with −6.5 K/km by day
  and with the model's own lapse rate by night, which carries its valley
  inversions (:func:`wxgrid.native.point_values`).
* **Night wind** — the night minimum also gets ``b · (ln v − mean ln v)``,
  ``v`` the members' mean night wind: the grids smooth out the radiative
  cooling of calm nights (autumn 2024: calm nights 0.9 ℃ too warm, windy ones
  unbiased), fitted on the same window.
* **Precipitation** — 12 h totals of the member mean, quantile-mapped from the
  forecast to the observed climatology of the window, recent days weighted
  more (14-day half-life).
* **Wind** — speed is the mean of the members' speeds times a ratio of
  means per period kind (the models run ~20 % light against these
  stations); direction from the averaged vector.
* **Probability** — logistic regression of "≥0.1 mm observed" on the
  members' quantile-mapped amounts (detail only); GEFS gives the 3-hourly
  detail. 中雨以上 (≥5 mm) and 大雨以上 (≥15 mm) per 12 h come from one
  cumulative logit over the three thresholds (shared slopes), so they stay
  ordered and the rare 15 mm events borrow the slope from the commoner ones.

Everything scored here is out of sample: :func:`backtest` refits for every
forecast day on periods that ended before it was issued.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import sys
from typing import Any

import numpy as np
import pandas as pd

from . import archive, native
from . import obs as obs_mod
from . import verify
from .postproc import qmap

MEMBERS = ("ifs", "aifs", "gfs")
DEFAULT_ROOT = pathlib.Path(os.environ.get("WXGRID_NATIVE_DIR", "/var/lib/wxgrid/native"))
WINDOW_DAYS = 60
OBS_DAYS = 62
#: Recency half-life of the temperature fit, days (None = every day in the window alike).
#: The members' night bias drifts with the season (+1.2 ℃ in March to −0.2 in May
#: 2025); 20 days cut the night bias that a flat 60-day mean left in May from
#: −0.51 to −0.37 ℃ and the mean MAE by 1.5 %.
HALF_LIFE_DAYS: float | None = 20.0
#: Same for the precipitation quantile maps and the PoP fit. With a flat window,
#: autumn 2024 still mapped October through a wetter September: rain frequency
#: bias 0.64 at day 1 and TS(>=5 mm) 12. 14 days: 0.71 and 18, with spring-summer
#: 2025 unchanged (PC 82.2 -> 82.5, TS 58.2 -> 58.9).
PRECIP_HALF_LIFE_DAYS: float | None = 14.0
#: Ridge toward equal weights, per lead day. Further out the members' skill
#: differences shrink against the noise of a 60-day fit: 1/1/2/4/8 against a flat 1
#: cut the day-5 MAE by 0.01–0.04 ℃ in all three backtest seasons, days 1–2 unchanged.
RIDGE = {1: 1.0, 2: 1.0, 3: 2.0, 4: 4.0, 5: 8.0}
MIN_ROWS = 30
LOWLAND_M = 500.0
WET_MM = verify.WET_MM
KEYS = ["init", "station", "date", "kind", "lead", "end"]
#: Night wind (m/s) is clipped to this range before its log enters the night term.
NIGHT_WIND_MS = (0.3, 8.0)
#: 12 h thresholds of the extra probabilities: 中雨以上, 大雨以上 (CMA 12 h grades).
HEAVY_MM = (5.0, 15.0)
#: The mapped member mean enters the heavy-rain logit capped here: uncapped, a
#: 30 mm forecast read as near-certain >=15 mm at the station (spring-summer 2025,
#: separate fits: 0.72 forecast on average above 50 %, 0.42 observed); capped and
#: cumulative, Brier skill for >=15 mm 0.14 -> 0.25.
HEAVY_CAP_MM = 10.0
#: Fewest events in the window to publish each extra probability.
HEAVY_MIN_EVENTS = {5.0: 20, 15.0: 8}


# ------------------------------------------------------------------ training table

def model_rows(run, sites: list[native.Site]) -> pd.DataFrame:
    """One run's period values at ``sites``: ``t`` (mixed lapse), ``t_std``, ``p``, ``w``."""
    if run.source == "gefs":
        f = pd.DataFrame(native.ensemble_period_rows(run, sites))
        return f.rename(columns={"f_pop": "pop", "f_pens": "pens"}).drop(columns="model")
    std = pd.DataFrame(native.period_rows(run, sites, lapse="std"))
    loc = pd.DataFrame(native.period_rows(run, sites, lapse="local"))
    out = std.rename(columns={"f_t": "t_std", "f_p": "p", "f_w": "w"}).drop(columns="model")
    out["t"] = np.where(out["kind"] == "day", out["t_std"], loc["f_t"].values)
    return out


def member_table(archive_root, sites: list[native.Site], *, since: dt.datetime | None = None,
                 until: dt.datetime | None = None, members=MEMBERS, with_gefs: bool = True) -> pd.DataFrame:
    """Wide table, one row per (init, station, period): ``<member>_t``, ``_t_std``, ``_p``, ``_w``
    and ``gefs_pop``/``gefs_pens``."""
    out = None
    for m in (*members, *(("gefs",) if with_gefs else ())):
        frames = []
        for init in archive.inits(archive_root, m):
            if (since and init < since) or (until and init > until):
                continue
            run = archive.load(archive_root, m, init)
            if run is not None:
                frames.append(model_rows(run, sites))
        if not frames:
            continue
        f = pd.concat(frames, ignore_index=True)
        f = f.rename(columns={c: f"{m}_{c}" for c in f.columns if c not in KEYS})
        out = f if out is None else out.merge(f, on=KEYS, how="outer")
    if out is None:
        return pd.DataFrame(columns=KEYS)
    for m in members:
        for c in ("t", "t_std", "p", "w"):
            if f"{m}_{c}" not in out:
                out[f"{m}_{c}"] = np.nan
    for c in ("gefs_pop", "gefs_pens"):
        if c not in out:
            out[c] = np.nan
    return out


def observed(obs_root, stations, dates: list[dt.date]) -> pd.DataFrame:
    """Observed 白天最高 / 夜间最低, 12 h precipitation and wind per (station, date, kind)."""
    store = obs_mod.ObsStore(obs_root)
    rows = []
    for st in stations:
        for (date, kind), v in verify.obs_periods(store.load(st.wmo), dates).items():
            rows.append({"station": st.id, "date": date, "kind": kind,
                         "o_t": v["t"], "o_p": v["precip"], "o_w": v["wind"]})
    return pd.DataFrame(rows, columns=["station", "date", "kind", "o_t", "o_p", "o_w"])


def training_table(archive_root, obs_root, stations=obs_mod.NEAR_YANSHAN, *,
                   since: dt.datetime | None = None, until: dt.datetime | None = None) -> pd.DataFrame:
    """Member values joined to observations, lowland stations only, sorted by init."""
    low = [s for s in stations if s.elevation_m < LOWLAND_M]
    tab = member_table(archive_root, native.sites(obs_mod.as_townships(low)), since=since, until=until)
    if tab.empty:
        return tab
    dates = sorted({dt.date.fromisoformat(d) for d in tab["date"]})
    tab = tab.merge(observed(obs_root, low, dates), on=["station", "date", "kind"], how="left")
    tab["init"] = pd.to_datetime(tab["init"])
    tab["end"] = pd.to_datetime(tab["end"])
    return tab.sort_values(["init", "station", "date", "kind"]).reset_index(drop=True)


# ------------------------------------------------------------------ temperature

def fit_temperature(tab: pd.DataFrame, *, members=MEMBERS, ridge: float | dict = RIDGE,
                    half_life: float | None = HALF_LIFE_DAYS) -> dict:
    """``{kind: {lead: {"w": {member: weight}, "a": intercept, "n": pairs}}}`` from rows with observations.

    Pairs are weighted by recency, ``0.5 ** (age / half_life)`` days before the
    newest one, so the fit follows a bias that drifts with the season.
    """
    cols = [f"{m}_t" for m in members]
    out: dict[str, dict[str, dict]] = {"day": {}, "night": {}}
    if tab.empty:
        return out
    good = tab.dropna(subset=[*cols, "o_t"])
    newest = good["end"].max() if len(good) else None
    for (kind, lead), g in good.groupby(["kind", "lead"]):
        if len(g) < MIN_ROWS:
            continue
        F = g[cols].values
        y = g["o_t"].values
        if half_life:
            age = (newest - g["end"]).dt.total_seconds().values / 86400.0
            s = 0.5 ** (age / half_life)
        else:
            s = np.ones(len(y))
        s = s / s.mean()
        fm = F.mean(axis=1)
        X = np.column_stack([np.ones(len(y)), (F - fm[:, None])[:, :-1]])
        lam = ridge.get(int(lead), max(ridge.values())) if isinstance(ridge, dict) else ridge
        R = lam * len(y) * np.eye(X.shape[1])
        R[0, 0] = 0.0
        beta = np.linalg.solve((X * s[:, None]).T @ X + R, (X * s[:, None]).T @ (y - fm))
        w = np.r_[beta[1:], 0.0] + 1.0 / len(cols)
        w[-1] = 1.0 - w[:-1].sum()
        w = np.clip(w, 0.0, None)
        w = w / w.sum() if w.sum() > 0 else np.full(len(cols), 1.0 / len(cols))
        a = float(np.average(y - F @ w, weights=s))
        out[kind][str(int(lead))] = {"w": {m: round(float(x), 4) for m, x in zip(members, w)},
                                     "a": round(a, 3), "n": int(len(g))}
    return out


def combine_temperature(cal_t: dict | None, kind: str, lead: int, values: dict[str, np.ndarray]) -> np.ndarray:
    """The consensus of member temperatures (any common shape) for one kind and lead day.

    Members missing at a point (NaN) drop out and the remaining weights are
    renormalised; with no fitted entry it is the plain mean.
    """
    entry = ((cal_t or {}).get(kind) or {}).get(str(int(lead)))
    names = list(values)
    stack = np.stack([np.asarray(values[m], dtype=float) for m in names])
    w = np.array([(entry["w"].get(m, 0.0) if entry else 1.0) for m in names], dtype=float)
    w = w.reshape((-1,) + (1,) * (stack.ndim - 1))
    ok = np.isfinite(stack)
    den = np.where(ok, w, 0.0).sum(axis=0)
    num = np.where(ok, w * np.nan_to_num(stack), 0.0).sum(axis=0)
    # all available members carry zero weight -> plain mean of what is there
    cnt = ok.sum(axis=0)
    plain = np.where(cnt > 0, np.where(ok, np.nan_to_num(stack), 0.0).sum(axis=0) / np.maximum(cnt, 1), np.nan)
    with np.errstate(invalid="ignore", divide="ignore"):
        out = np.where(den > 1e-9, num / np.where(den > 1e-9, den, 1.0), plain)
    return out + (entry["a"] if entry else 0.0)


def _night_wind_x(wind) -> np.ndarray:
    return np.log(np.clip(np.asarray(wind, dtype=float), *NIGHT_WIND_MS))


def fit_night_wind(tab: pd.DataFrame, base: np.ndarray, *, half_life: float | None = HALF_LIFE_DAYS) -> dict | None:
    """Slope of the night residual (observed − ``base``) on ln(member-mean night wind).

    Pooled over leads and recency-weighted like the temperature fit; centred on
    the window's mean wind, so it moves calm and windy nights apart without
    shifting the average (the consensus's own bias stays in charge of that).
    """
    night = (tab["kind"] == "night").values
    wind = tab[[f"{m}_w" for m in MEMBERS]].mean(axis=1).values
    r = tab["o_t"].values.astype(float) - np.asarray(base, dtype=float)
    ok = night & np.isfinite(r) & np.isfinite(wind)
    if ok.sum() < 100:
        return None
    g = tab[ok]
    x, r = _night_wind_x(wind[ok]), r[ok]
    w = recency_weights(g, half_life)
    mu, rm = np.average(x, weights=w), np.average(r, weights=w)
    sxx = np.sum(w * (x - mu) ** 2)
    if sxx <= 0:
        return None
    # 1 % shrinkage: a near-constant wind in the window must not blow the slope up
    b = float(np.sum(w * (x - mu) * (r - rm)) / (1.01 * sxx))
    return {"b": round(b, 4), "mu": round(float(mu), 4), "n": int(ok.sum())}


def night_wind_adjust(entry: dict | None, wind) -> np.ndarray:
    """The night term for member-mean wind ``wind`` (m/s); 0 where unknown or unfitted."""
    wind = np.asarray(wind, dtype=float)
    if not entry:
        return np.zeros(wind.shape)
    with np.errstate(invalid="ignore"):
        adj = entry["b"] * (_night_wind_x(wind) - entry["mu"])
    return np.where(np.isfinite(adj), adj, 0.0)


def consensus_t(cal: dict | None, tab: pd.DataFrame, *, night_wind: bool = True) -> np.ndarray:
    """Consensus period temperature for every row of a member table (with the night term)."""
    out = np.full(len(tab), np.nan)
    pos = {ix: j for j, ix in enumerate(tab.index)}
    for (kind, lead), g in tab.groupby(["kind", "lead"]):
        j = np.array([pos[ix] for ix in g.index])
        t = combine_temperature((cal or {}).get("temp"), kind, int(lead), {m: g[f"{m}_t"].values for m in MEMBERS})
        if night_wind and kind == "night":
            t = t + night_wind_adjust((cal or {}).get("night_wind"), g[[f"{m}_w" for m in MEMBERS]].mean(axis=1).values)
        out[j] = t
    return out


# ------------------------------------------------------------------ precipitation

def _wquantile(x: np.ndarray, w: np.ndarray, p: np.ndarray) -> np.ndarray:
    """Weighted quantiles (``np.quantile``'s linear rule when all weights are equal)."""
    order = np.argsort(x, kind="stable")
    x, w = x[order], w[order]
    cw = np.cumsum(w)
    pos = (cw - w[0]) / (cw[-1] - w[0]) if cw[-1] > w[0] else np.linspace(0.0, 1.0, x.size)
    return np.interp(p, pos, x)


def qmap_fit(f, o, n: int = 401, weights=None) -> dict | None:
    """Forecast and observed climatologies of 12 h totals, in :func:`wxgrid.postproc.qmap` form.

    ``weights`` (e.g. recency) weight each pair in both climatologies.
    """
    f, o = np.asarray(f, dtype=float), np.asarray(o, dtype=float)
    w = np.ones(f.size) if weights is None else np.asarray(weights, dtype=float)
    ok = np.isfinite(f) & np.isfinite(o) & np.isfinite(w)
    f, o, w = f[ok], o[ok], w[ok]
    if f.size < 60:
        return None
    p = np.linspace(0.0, 1.0, n)
    return {"p": p.round(5).tolist(), "fq": _wquantile(f, w, p).round(3).tolist(),
            "oq": _wquantile(o, w, p).round(3).tolist(), "n": int(f.size)}


def recency_weights(tab: pd.DataFrame, half_life: float | None) -> np.ndarray:
    """``0.5 ** (age / half_life)`` with age in days before the newest period in ``tab``."""
    if not half_life or tab.empty:
        return np.ones(len(tab))
    age = (tab["end"].max() - tab["end"]).dt.total_seconds().values / 86400.0
    return 0.5 ** (age / half_life)


def member_mean_precip(tab: pd.DataFrame, members=MEMBERS) -> pd.Series:
    return tab[[f"{m}_p" for m in members]].mean(axis=1, skipna=False)


def fit_precip(tab: pd.DataFrame, *, members=MEMBERS, half_life: float | None = None) -> dict:
    mean = member_mean_precip(tab, members)
    w = recency_weights(tab, half_life if half_life is not None else PRECIP_HALF_LIFE_DAYS)
    return {k: qmap_fit(mean[tab["kind"] == k], tab.loc[tab["kind"] == k, "o_p"], weights=w[(tab["kind"] == k).values])
            for k in ("day", "night")}


def map_precip(cal_p: dict | None, kind: str, total) -> np.ndarray:
    return qmap(total, (cal_p or {}).get(kind))


# ------------------------------------------------------------------ wind

WIND_RATIO = (0.6, 1.8)


def member_mean_wind(tab: pd.DataFrame, members=MEMBERS) -> pd.Series:
    return tab[[f"{m}_w" for m in members]].mean(axis=1, skipna=False)


def fit_wind(tab: pd.DataFrame, *, members=MEMBERS) -> dict:
    """Ratio of observed to member-mean wind per kind (pooled leads), clipped to :data:`WIND_RATIO`."""
    out = {}
    f = member_mean_wind(tab, members)
    for kind in ("day", "night"):
        s = (tab["kind"] == kind) & f.notna() & tab["o_w"].notna()
        if s.sum() >= 100 and f[s].mean() > 0.3:
            out[kind] = round(float(np.clip(tab.loc[s, "o_w"].mean() / f[s].mean(), *WIND_RATIO)), 3)
    return out


def wind_factor(cal: dict | None, kind: str) -> float:
    return float(((cal or {}).get("wind") or {}).get(kind, 1.0))


# ------------------------------------------------------------------ probability

def _logit_fit(X: np.ndarray, y: np.ndarray, l2: float = 1.0, iters: int = 50, sample_weight=None) -> np.ndarray:
    X1 = np.column_stack([np.ones(len(y)), X])
    s = np.ones(len(y)) if sample_weight is None else np.asarray(sample_weight, dtype=float) / np.mean(sample_weight)
    w = np.zeros(X1.shape[1])
    pen = l2 * np.r_[0.0, np.ones(len(w) - 1)]
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-np.clip(X1 @ w, -30, 30)))
        g = X1.T @ (s * (p - y)) + pen * w
        H = (X1 * (s * p * (1 - p))[:, None]).T @ X1 + np.diag(pen)
        step = np.linalg.solve(H, g)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return w


def pop_features(member_totals: dict[str, np.ndarray], cal_pm: dict | None, kind: str) -> np.ndarray:
    """[share of members wet after their own quantile map, log1p of their mapped mean amount]."""
    fits = (cal_pm or {}).get(kind) or {}
    mapped = np.stack([qmap(np.asarray(v, dtype=float).ravel(), fits.get(m)) for m, v in member_totals.items()])
    with np.errstate(invalid="ignore"):
        wet = np.nanmean(np.where(np.isfinite(mapped), mapped >= WET_MM, np.nan), axis=0)
        lm = np.log1p(np.nanmean(mapped, axis=0))
    return np.column_stack([wet, lm])


def fit_pop(tab: pd.DataFrame, *, members=MEMBERS, half_life: float | None = None) -> dict:
    """Per kind: each member's quantile map and logistic coefficients on :func:`pop_features`.

    The GEFS share was tried as a third predictor and added nothing (Brier skill
    0.69 vs 0.70 at day 1 in the 2025 backtest), so the period probability does
    not depend on the ensemble being available.
    """
    out: dict[str, Any] = {"member_qmap": {}, "logit": {}}
    for kind in ("day", "night"):
        g = tab[tab["kind"] == kind].dropna(subset=["o_p", *[f"{m}_p" for m in members]])
        w = recency_weights(g, half_life if half_life is not None else PRECIP_HALF_LIFE_DAYS)
        out["member_qmap"][kind] = {m: qmap_fit(g[f"{m}_p"], g["o_p"], weights=w) for m in members}
        if len(g) < 80:
            continue
        X = pop_features({m: g[f"{m}_p"].values for m in members}, out["member_qmap"], kind)
        out["logit"][kind] = [round(float(x), 5) for x in
                              _logit_fit(X, (g["o_p"].values >= WET_MM).astype(float), sample_weight=w)]
    return out


def predict_pop(cal_pop: dict | None, kind: str, member_totals: dict[str, np.ndarray], fallback=None) -> np.ndarray:
    """Probability (0–1) of ≥0.1 mm in the period.

    Without a fit: ``fallback`` (e.g. the GEFS share) if given, else the share of
    members with ≥0.1 mm.
    """
    shape = np.shape(next(iter(member_totals.values())))
    coef = ((cal_pop or {}).get("logit") or {}).get(kind)
    X = pop_features(member_totals, (cal_pop or {}).get("member_qmap"), kind)
    if not coef:
        if fallback is not None:
            return np.asarray(fallback, dtype=float).reshape(shape)
        return X[:, 0].reshape(shape)
    p = 1.0 / (1.0 + np.exp(-np.clip(np.column_stack([np.ones(len(X)), X]) @ np.asarray(coef), -30, 30)))
    return np.where(np.isfinite(X).all(axis=1), p, np.nan).reshape(shape)


def heavy_features(member_totals: dict[str, np.ndarray], cal_pm: dict | None, kind: str) -> np.ndarray:
    """[ln(1 + mapped member mean, capped at :data:`HEAVY_CAP_MM`), share of members ≥0.1 mm,
    share ≥5 mm (each after its own quantile map), night flag]."""
    fits = (cal_pm or {}).get(kind) or {}
    mapped = np.stack([qmap(np.asarray(v, dtype=float).ravel(), fits.get(m)) for m, v in member_totals.items()])
    ok = np.isfinite(mapped)
    with np.errstate(invalid="ignore"):
        mean = np.nanmean(mapped, axis=0)
        share = [np.nanmean(np.where(ok, mapped >= thr, np.nan), axis=0) for thr in (WET_MM, HEAVY_MM[0])]
    return np.column_stack([np.log1p(np.minimum(mean, HEAVY_CAP_MM)), *share,
                            np.full(mean.shape, float(kind == "night"))])


def fit_heavy(tab: pd.DataFrame, cal_pm: dict | None, *, members=MEMBERS, half_life: float | None = None) -> dict:
    """Cumulative logit ``P(obs ≥ c_j) = σ(a + δ_j + β·x)`` over c = 0.1, 5, 15 mm, both kinds pooled.

    One slope vector for every threshold (fitted on the rows stacked once per
    threshold, an indicator per threshold above the first): the 15 mm events
    are few, the slope comes mostly from the commoner ones. Thresholds with
    fewer than :data:`HEAVY_MIN_EVENTS` events in the window are not published.
    """
    g = tab.dropna(subset=["o_p", *[f"{m}_p" for m in members]])
    if g.empty:
        return {}
    X = np.vstack([heavy_features({m: g.loc[g["kind"] == k, f"{m}_p"].values for m in members}, cal_pm, k)
                   for k in ("day", "night")])
    g = pd.concat([g[g["kind"] == "day"], g[g["kind"] == "night"]])
    o = g["o_p"].values.astype(float)
    events = {thr: int((o >= thr).sum()) for thr in HEAVY_MM}
    if events[HEAVY_MM[0]] < HEAVY_MIN_EVENTS[HEAVY_MM[0]] or not np.isfinite(X).all():
        return {"events": {str(k): v for k, v in events.items()}}
    w = recency_weights(g, half_life if half_life is not None else PRECIP_HALF_LIFE_DAYS)
    cuts = (WET_MM, *HEAVY_MM)
    rows, ys = [], []
    for j, thr in enumerate(cuts):
        ind = np.zeros((len(o), len(cuts) - 1))
        if j:
            ind[:, j - 1] = 1.0
        rows.append(np.column_stack([X, ind]))
        ys.append((o >= thr).astype(float))
    coef = _logit_fit(np.vstack(rows), np.concatenate(ys), sample_weight=np.tile(w, len(cuts)))
    return {"coef": [round(float(c), 5) for c in coef], "events": {str(k): v for k, v in events.items()},
            "published": [thr for thr in HEAVY_MM if events[thr] >= HEAVY_MIN_EVENTS[thr]]}


def predict_heavy(cal_heavy: dict | None, cal_pm: dict | None, kind: str,
                  member_totals: dict[str, np.ndarray]) -> dict[float, np.ndarray | None]:
    """Probability (0–1) of ≥5 and ≥15 mm in the period; None for a threshold not published."""
    shape = np.shape(next(iter(member_totals.values())))
    coef = (cal_heavy or {}).get("coef")
    out: dict[float, np.ndarray | None] = {thr: None for thr in HEAVY_MM}
    if not coef:
        return out
    X = heavy_features(member_totals, cal_pm, kind)
    base = np.column_stack([np.ones(len(X)), X]) @ np.asarray(coef[:1 + X.shape[1]])
    for j, thr in enumerate(HEAVY_MM):
        if thr not in (cal_heavy.get("published") or []):
            continue
        p = 1.0 / (1.0 + np.exp(-np.clip(base + coef[1 + X.shape[1] + j], -30, 30)))
        out[thr] = np.where(np.isfinite(X).all(axis=1), p, np.nan).reshape(shape)
    return out


# ------------------------------------------------------------------ fit / apply

def fit(tab: pd.DataFrame, *, now: dt.datetime | None = None, window_days: int = WINDOW_DAYS) -> dict[str, Any]:
    """Calibration from the rows whose periods ended before ``now``, over ``window_days``."""
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    tr = tab[(tab["end"] <= now) & (tab["end"] > now - dt.timedelta(days=window_days))] if len(tab) else tab
    dates = sorted(tr["date"].unique()) if len(tr) else []
    temp = fit_temperature(tr)
    pop = fit_pop(tr) if len(tr) else {}
    return {
        "method": "weighted member consensus (weights >= 0, sum 1, ridge to equal) + bias, per kind and lead; "
                  "night term on ln(wind); 12 h precipitation quantile map; logistic PoP; "
                  "cumulative logit for >=5 / >=15 mm",
        "members": list(MEMBERS), "window": [dates[0], dates[-1]] if dates else None,
        "n_pairs": int(tr["o_t"].notna().sum()) if len(tr) else 0,
        "lapse": {"day": "std", "night": "local"},
        "temp": temp,
        "night_wind": fit_night_wind(tr, consensus_t({"temp": temp}, tr, night_wind=False)) if len(tr) else None,
        "precip": fit_precip(tr) if len(tr) else {}, "pop": pop,
        "heavy": fit_heavy(tr, pop.get("member_qmap")) if len(tr) else {},
        "wind": fit_wind(tr) if len(tr) else {},
    }


def predict(cal: dict | None, tab: pd.DataFrame) -> pd.DataFrame:
    """Consensus ``t``, ``p``, ``pop``, ``p5``/``p15`` (≥5 / ≥15 mm) and ``w`` for every row of a member table."""
    out = pd.DataFrame(index=tab.index, columns=["t", "p", "pop", "p5", "p15", "w"], dtype=float)
    out["t"] = consensus_t(cal, tab)
    cal_pop = (cal or {}).get("pop") or {}
    for kind, g in tab.groupby("kind"):
        # averages over the members present, as the live product does with one short
        # (the backtest scores complete rows only, where this is the plain mean)
        out.loc[g.index, "w"] = g[[f"{m}_w" for m in MEMBERS]].mean(axis=1).values * wind_factor(cal, kind)
        out.loc[g.index, "p"] = map_precip((cal or {}).get("precip"), kind,
                                           g[[f"{m}_p" for m in MEMBERS]].mean(axis=1).values)
        totals = {m: g[f"{m}_p"].values for m in MEMBERS}
        out.loc[g.index, "pop"] = predict_pop(cal_pop, kind, totals,
                                              g["gefs_pop"].values if "gefs_pop" in g else None)
        heavy = predict_heavy((cal or {}).get("heavy"), cal_pop.get("member_qmap"), kind, totals)
        for thr, col in zip(HEAVY_MM, ("p5", "p15")):
            if heavy[thr] is not None:
                out.loc[g.index, col] = heavy[thr]
    out["p5"], out["p15"] = order_probabilities(out["pop"].values, out["p5"].values, out["p15"].values)
    return out


def order_probabilities(pop, p5, p15):
    """Keep P(≥15 mm) ≤ P(≥5 mm) ≤ P(≥0.1 mm) where the larger one is known (two separate fits)."""
    pop, p5, p15 = (np.asarray(a, dtype=float) for a in (pop, p5, p15))
    p5 = np.where(np.isfinite(pop) & np.isfinite(p5), np.minimum(p5, pop), p5)
    p15 = np.where(np.isfinite(p5) & np.isfinite(p15), np.minimum(p15, p5), p15)
    return p5, p15


# ------------------------------------------------------------------ backtest / scores

def _ewma_bias(tr: pd.DataFrame, col: str, alpha: float = 0.1) -> dict:
    out = {}
    d = tr.assign(e=tr[col] - tr["o_t"]).dropna(subset=["e"])
    for key, g in d.groupby(["kind", "lead"]):
        b = None
        for e in g.groupby("end")["e"].mean().sort_index().values:
            b = e if b is None else (1 - alpha) * b + alpha * e
        out[(key[0], int(key[1]))] = b or 0.0
    return out


def _wind(f, o) -> dict:
    from .phenomena import beaufort
    sc = verify.wind_scores(f, o)
    if f.size:
        sc["force_exact"] = float(np.mean([beaufort(a) == beaufort(b) for a, b in zip(f, o)]) * 100)
    return sc


def _scores(f_t, f_p, rows: pd.DataFrame, f_w=None) -> dict[int, dict]:
    out = {}
    for lead in verify.LEADS:
        s = (rows["lead"] == lead).values
        day, ngt = s & (rows["kind"] == "day").values, s & (rows["kind"] == "night").values

        def pair(f, o, m):
            ok = m & np.isfinite(f) & np.isfinite(o)
            return f[ok], o[ok]
        o_t, o_p = rows["o_t"].values.astype(float), rows["o_p"].values.astype(float)
        fp, op = pair(f_p, o_p, s)
        out[lead] = {"tmax": verify.temp_scores(*pair(f_t, o_t, day)),
                     "tmin": verify.temp_scores(*pair(f_t, o_t, ngt)),
                     "rain": verify.rain_scores(fp, op),
                     "rain5": verify.rain_scores(fp, op, f_thr=verify.MODERATE_12H_MM, o_thr=verify.MODERATE_12H_MM)}
        if f_w is not None:
            out[lead]["wind"] = _wind(*pair(np.asarray(f_w, dtype=float), rows["o_w"].values.astype(float), s))
    return out


def prob_scores(f, o_amount, thr: float, clim: float, mask, lead=None, *,
                bins=((0.0, 0.2), (0.2, 0.5), (0.5, 0.8), (0.8, 1.01)), min_n: int = 50,
                min_events: int = 0) -> dict | None:
    """Brier score, skill against the climatological frequency ``clim`` and reliability of
    probabilities ``f`` (0–1) for "observed amount ≥ ``thr``"; ``bss_by_lead`` when ``lead`` is given.
    With fewer than ``min_events`` events the skill is not computed (None): one wet day decides it."""
    f, o_amount = np.asarray(f, dtype=float), np.asarray(o_amount, dtype=float)
    ok = np.asarray(mask, dtype=bool) & np.isfinite(f) & np.isfinite(o_amount)
    if ok.sum() < min_n:
        return None
    fo, o = f[ok], (o_amount[ok] >= thr).astype(float)
    bs, bc = float(((fo - o) ** 2).mean()), float(((clim - o) ** 2).mean())
    enough = o.sum() >= min_events
    out = {"n": int(ok.sum()), "events": int(o.sum()), "brier": bs, "brier_climatology": bc,
           "bss": 1 - bs / bc if bc > 0 and enough else None,
           "reliability": [{"bin": [a, min(b, 1.0)], "n": int(((fo >= a) & (fo < b)).sum()),
                            "observed": float(o[(fo >= a) & (fo < b)].mean()) if ((fo >= a) & (fo < b)).any() else None}
                           for a, b in bins]}
    if lead is not None and enough:
        lo = np.asarray(lead)[ok]
        by = {}
        for ld in verify.LEADS:
            s = lo == ld
            c1 = float(((clim - o[s]) ** 2).mean()) if s.sum() >= 20 and o[s].sum() >= min_events / 2 else 0.0
            by[str(ld)] = 1 - float(((fo[s] - o[s]) ** 2).mean()) / c1 if c1 > 0 else None
        out["bss_by_lead"] = by
    return out


#: Night minimum at or below this is a frost night (the product's 霜冻 alert).
FROST_C = 0.0


def frost_scores(f_t, rows: pd.DataFrame) -> dict | None:
    """Per lead: frost nights observed, hit, missed and falsely forecast (minimum ≤ :data:`FROST_C`)."""
    f = np.asarray(f_t, dtype=float)
    o = rows["o_t"].values.astype(float)
    night = (rows["kind"] == "night").values & np.isfinite(f) & np.isfinite(o)
    if not (night & (o <= FROST_C)).any() and not (night & (f <= FROST_C)).any():
        return None
    out = {}
    for ld in verify.LEADS:
        s = night & (rows["lead"].values == ld)
        ev, fc = o[s] <= FROST_C, f[s] <= FROST_C
        out[str(ld)] = {"n": int(s.sum()), "events": int(ev.sum()), "hits": int((ev & fc).sum()),
                        "false_alarms": int((fc & ~ev).sum()),
                        "bias_on_events": float(np.mean(f[s][ev] - o[s][ev])) if ev.any() else None}
    return out


def backtest(tab: pd.DataFrame, *, test_days: int = 30, end: dt.datetime | None = None,
             window_days: int = WINDOW_DAYS) -> dict[str, Any]:
    """Out-of-sample scores over the last ``test_days`` of issues, refitting before each one.

    Configurations, all on the same rows (every member present, observation available):
    ``new`` this consensus; ``new_raw`` the plain member mean (standard lapse,
    no correction); ``mean_bias`` the plain mean with one decaying-average bias
    per kind and lead (how the Open-Meteo engine corrects); ``ifs`` ECMWF IFS alone.
    """
    if tab.empty:
        return {"error": "no archived runs yet", "days": 0}
    end = end or tab["init"].max()
    inits = sorted(i for i in tab["init"].unique() if end - pd.Timedelta(days=test_days) < i <= end)
    if len(inits) < 10 or (tab["end"].min() > inits[0] - pd.Timedelta(days=20)):
        return {"error": "not enough verified days yet", "days": len(inits)}
    preds = []
    for init in inits:
        issue = pd.Timestamp(init) + pd.Timedelta(hours=native.ISSUE_DELAY_H)
        test = tab[tab["init"] == init]
        cal = fit(tab, now=issue.to_pydatetime(), window_days=window_days)
        p = predict(cal, test)
        tr = tab[(tab["end"] <= issue) & (tab["end"] > issue - pd.Timedelta(days=window_days))]
        mean_raw = test[[f"{m}_t_std" for m in MEMBERS]].mean(axis=1, skipna=False)
        tr_mean = tr.assign(mean=tr[[f"{m}_t_std" for m in MEMBERS]].mean(axis=1, skipna=False))
        b = _ewma_bias(tr_mean, "mean")
        mean_bias = mean_raw - np.array([b.get((k, int(ld)), 0.0) for k, ld in zip(test["kind"], test["lead"])])
        preds.append(pd.DataFrame({"new_t": p["t"], "new_p": p["p"], "pop": p["pop"], "p5": p["p5"], "p15": p["p15"],
                                   "new_w": p["w"],
                                   "raw_t": mean_raw, "mb_t": mean_bias, "raw_w": member_mean_wind(test),
                                   "raw_p": member_mean_precip(test)}, index=test.index))
    P = pd.concat(preds)
    rows = tab.loc[P.index]
    # every configuration is scored on the same rows: all members present and observed
    tmask = rows[["o_t", *[f"{m}_t" for m in MEMBERS], *[f"{m}_t_std" for m in MEMBERS]]].notna().all(axis=1).values
    pmask = rows[["o_p", *[f"{m}_p" for m in MEMBERS]]].notna().all(axis=1).values

    def t_(x):
        return np.where(tmask, np.asarray(x, dtype=float), np.nan)

    def p_(x):
        return np.where(pmask, np.asarray(x, dtype=float), np.nan)

    wmask = rows[["o_w", *[f"{m}_w" for m in MEMBERS]]].notna().all(axis=1).values

    def w_(x):
        return np.where(wmask, np.asarray(x, dtype=float), np.nan)

    configs = {
        "new": {"label": "本系统：3 家模式加权 + 站点订正",
                "scores": _scores(t_(P["new_t"]), p_(P["new_p"]), rows, w_(P["new_w"]))},
        "mean_bias": {"label": "3 家平均 + 统一偏差订正", "scores": _scores(t_(P["mb_t"]), p_(P["new_p"]), rows)},
        "new_raw": {"label": "3 家模式平均（未订正）",
                    "scores": _scores(t_(P["raw_t"]), p_(P["raw_p"]), rows, w_(P["raw_w"]))},
        "ifs": {"label": "单一 ECMWF IFS", "scores": _scores(t_(rows["ifs_t_std"]), p_(rows["ifs_p"]), rows,
                                                            w_(rows["ifs_w"]))},
    }
    before = tab.loc[tab["end"] < rows["init"].min(), "o_p"].dropna()
    pop = prob_scores(P["pop"].values, rows["o_p"].values, WET_MM, float((before >= WET_MM).mean()),
                      pmask, rows["lead"].values)
    heavy = {}
    for thr, col in zip(HEAVY_MM, ("p5", "p15")):
        sc = prob_scores(P[col].values, rows["o_p"].values, thr, float((before >= thr).mean()), pmask,
                         rows["lead"].values, bins=((0.0, 0.1), (0.1, 0.3), (0.3, 0.5), (0.5, 1.01)), min_events=10)
        if sc:
            heavy[f"{thr:g}"] = sc
    first, last = rows["init"].min(), rows["init"].max()
    return {
        "window": [(first + pd.Timedelta(days=1)).date().isoformat(), (last + pd.Timedelta(days=1)).date().isoformat()],
        "days": len(inits), "stations": sorted(rows["station"].unique().tolist()),
        "order": ["new", "mean_bias", "new_raw", "ifs"], "baseline": "ifs",
        "configs": configs, "pop": pop, "pop_label": "3 家模式雨量经逻辑回归校准",
        "heavy": heavy or None,
        "frost": frost_scores(t_(P["new_t"]), rows) or None,
        "notes": "白天最高对比 12 时（UTC）报的 24 小时最高气温，夜间最低对比 00 时报的 24 小时最低气温；"
                 "降水为 12 小时（白天 08—20 时、夜间 20—08 时），≥0.1 mm 为有雨，微量按无雨。"
                 "全部样本外：每次预报只用发布前已结束时段的实况拟合。",
    }


# ------------------------------------------------------------------ what was actually issued

#: Live scores cover this many days of issues; :data:`HEALTH_LIVE_DAYS` for the health check.
LIVE_DAYS = 30
HEALTH_LIVE_DAYS = 14


def issued_rows(runs: dict, stations, cal: dict | None, *, issue: dt.datetime, run_key: str) -> list[dict]:
    """The forecast this issue makes at the lowland stations, as the backtest would score it.

    ``runs`` maps member key to the :class:`~wxgrid.gribbox.BoxRun` used (same
    cycle). Rows carry the members present, so a forecast made with one short is
    scored as issued.
    """
    low = [s for s in stations if s.elevation_m < LOWLAND_M]
    if not low or not runs:
        return []
    sites = native.sites(obs_mod.as_townships(low))
    tab = None
    for m, run in runs.items():
        f = model_rows(run, sites)
        f = f.rename(columns={c: f"{m}_{c}" for c in f.columns if c not in KEYS})
        tab = f if tab is None else tab.merge(f, on=KEYS, how="outer")
    for m in MEMBERS:
        for c in ("t", "t_std", "p", "w"):
            if f"{m}_{c}" not in tab:
                tab[f"{m}_{c}"] = np.nan
    tab = tab.reset_index(drop=True)
    p = predict(cal, tab)
    out = []
    for i, r in tab.iterrows():
        end = dt.datetime.fromisoformat(str(r["end"]))
        if end <= issue + dt.timedelta(hours=6):
            continue                     # already (mostly) over when issued
        rec = {"run": run_key, "issue": issue.isoformat(timespec="minutes"), "init": str(r["init"])[:13],
               "members": sorted(m for m in runs if np.isfinite(r.get(f"{m}_t", np.nan))),
               "station": r["station"], "date": r["date"], "kind": r["kind"], "end": end.isoformat(),
               "lead": native.lead_day(end, issue)}
        for c in ("t", "p", "pop", "p5", "p15", "w"):
            v = float(p.at[i, c])
            rec[c] = round(v, 3) if np.isfinite(v) else None
        out.append(rec)
    return out


def log_issued(root, rows: list[dict]) -> int:
    """Append to ``<root>/issued/<YYYYMM>.jsonl`` (by issue month); kept for good, ~6 MB a year."""
    if not rows:
        return 0
    d = pathlib.Path(root) / "issued"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{rows[0]['issue'][:4]}{rows[0]['issue'][5:7]}.jsonl", "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


def load_issued(root, *, since: dt.datetime | None = None) -> pd.DataFrame:
    """Every logged row issued at or after ``since``; a re-issue for the same run replaces the earlier one."""
    d = pathlib.Path(root) / "issued"
    rows = []
    for p in sorted(d.glob("*.jsonl")) if d.is_dir() else []:
        if since and p.stem < f"{since:%Y%m}":
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    if not rows:
        return pd.DataFrame()
    f = pd.DataFrame(rows)
    f["issue"] = pd.to_datetime(f["issue"], format="ISO8601")
    f["end"] = pd.to_datetime(f["end"], format="ISO8601")
    if since is not None:
        f = f[f["issue"] >= pd.Timestamp(since)]
    return f.drop_duplicates(subset=["run", "station", "date", "kind"], keep="last").reset_index(drop=True)


def _block(f: pd.DataFrame) -> dict[int, dict]:
    out = {}
    for lead in verify.LEADS:
        g = f[f["lead"] == lead]
        day, ngt = g[g["kind"] == "day"], g[g["kind"] == "night"]

        def arr(x, a, b):
            x = x[[a, b]].astype(float).dropna()
            return x[a].values, x[b].values
        fp, op = arr(g, "p", "o_p")
        out[lead] = {"tmax": verify.temp_scores(*arr(day, "t", "o_t")), "tmin": verify.temp_scores(*arr(ngt, "t", "o_t")),
                     "rain": verify.rain_scores(fp, op),
                     "rain5": verify.rain_scores(fp, op, f_thr=verify.MODERATE_12H_MM, o_thr=verify.MODERATE_12H_MM)}
    return out


def live_scores(root, obs_root, stations=obs_mod.NEAR_YANSHAN, *, now: dt.datetime | None = None,
                days: int = LIVE_DAYS) -> dict[str, Any] | None:
    """Scores of the forecasts actually issued over the last ``days`` (periods already observed),
    plus a monthly history of the day-1 scores from the whole log."""
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    f = load_issued(root)
    if f.empty:
        return None
    low = [s for s in stations if s.elevation_m < LOWLAND_M]
    by_id = {obs_mod.as_townships([s])[0].id: s for s in low}
    f = f[f["station"].isin(by_id) & (f["end"] <= pd.Timestamp(now))]
    if f.empty:
        return None
    dates = sorted({dt.date.fromisoformat(d) for d in f["date"]})
    f = f.merge(observed(obs_root, low, dates), on=["station", "date", "kind"], how="left")
    f["month"] = f["issue"].dt.strftime("%Y-%m")
    months = {}
    for mon, g in f.groupby("month"):
        s = _block(g)[1]
        months[mon] = {"issues": int(g["run"].nunique()), "tmax_mae": s["tmax"].get("mae"),
                       "tmin_mae": s["tmin"].get("mae"), "rain_pc": s["rain"].get("pc"),
                       "tmax_n": s["tmax"].get("n"), "tmin_n": s["tmin"].get("n")}
    recent = f[f["issue"] > pd.Timestamp(now - dt.timedelta(days=days))]
    if recent.empty:
        return {"months": months}
    pop = prob_scores(recent["pop"].values, recent["o_p"].values, WET_MM,
                      float((f.loc[f["issue"] <= recent["issue"].min(), "o_p"].dropna() >= WET_MM).mean())
                      if (f["issue"] <= recent["issue"].min()).any() else 0.5,
                      np.ones(len(recent), bool), recent["lead"].values, min_n=30)
    runs = recent.drop_duplicates("run")
    short = int((runs["members"].map(len) < len(MEMBERS)).sum())
    return {
        "window": [recent["issue"].min().date().isoformat(), recent["issue"].max().date().isoformat()],
        "issues": int(len(runs)), "short_member_issues": short,
        "scores": _block(recent), "pop": pop,
        "frost": frost_scores(recent["t"].values, recent) or None,
        "months": months,
        "notes": "按每次实际发布的预报（含当时缺成员、订正未更新等情况）在周边国家站位置打分。",
    }


def health(cal: dict | None, scores: dict | None, *, members_used, members=MEMBERS,
           now: dt.datetime | None = None, obs_latest: dt.datetime | None = None) -> list[str]:
    """Plain-language warnings for the page when the system is running below its tested state."""
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    out = []
    missing = [m for m in members if m not in members_used]
    if missing:
        from .sources import raw
        out.append(f"本次未取到 {'、'.join(raw.MODELS[m].label for m in missing)}，按其余 {len(members_used)} 家出预报，误差可能略大。")
    if not cal:
        out.append("站点订正未生效（模式存档或实况不足），本次为各家简单平均，气温误差会明显偏大。")
    elif cal.get("generated"):
        age = (now - dt.datetime.fromisoformat(cal["generated"]).replace(tzinfo=None)).total_seconds() / 86400
        if age >= 2:
            out.append(f"站点订正已 {age:.0f} 天未更新（实况或存档刷新失败），沿用上次的订正。")
    if obs_latest is not None and (now - obs_latest).total_seconds() > 3 * 86400:
        out.append(f"周边国家站实况已 {(now - obs_latest).total_seconds() / 86400:.0f} 天未更新。")
    live = (scores or {}).get("live") or {}
    bt = (scores or {}).get("configs", {}).get("new", {}).get("scores", {})
    lv = (live.get("scores") or {})
    l1, b1 = lv.get(1) or lv.get("1"), bt.get(1) or bt.get("1")
    if l1 and b1 and live.get("issues", 0) >= 10:
        def mae(s, k):
            return (s.get(k) or {}).get("mae")
        gap = [mae(l1, k) - mae(b1, k) for k in ("tmax", "tmin") if mae(l1, k) is not None and mae(b1, k) is not None]
        if gap and max(gap) > 0.5:
            out.append(f"最近 {LIVE_DAYS} 天实际发布的预报比同期回测误差大 {max(gap):.1f} ℃，请检查数据与订正是否正常。")
    return out


# ------------------------------------------------------------------ daily refresh

def finite(obj):
    """``obj`` with NaN/inf floats (empty score cells) as None: browsers' JSON.parse rejects NaN."""
    if isinstance(obj, dict):
        return {(str(k) if not isinstance(k, str) else k): finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [finite(v) for v in obj]
    if isinstance(obj, (float, np.floating)):
        return float(obj) if np.isfinite(obj) else None
    if isinstance(obj, np.integer):
        return int(obj)
    return obj


def refresh(root=DEFAULT_ROOT, *, archive_root=archive.DEFAULT_ROOT, obs_root=None,
            stations=obs_mod.NEAR_YANSHAN, sess=None, now: dt.datetime | None = None,
            max_age_days: float = 1.0, update_obs: bool = True) -> dict[str, Any] | None:
    """Update observations, refit and rescore at most once per ``max_age_days``.

    Returns the calibration in force: the new one, the previous one when the
    refresh failed and it is under a week old, else None (plain member mean).
    """
    root = pathlib.Path(root)
    root.mkdir(parents=True, exist_ok=True)
    obs_root = pathlib.Path(obs_root) if obs_root else root.parent / "verify" / "obs"
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    cal_path = root / "calibration.json"
    cur = json.loads(cal_path.read_text(encoding="utf-8")) if cal_path.exists() else None
    if cur and cur.get("generated"):
        age = now - dt.datetime.fromisoformat(cur["generated"]).replace(tzinfo=None)
        if age < dt.timedelta(days=max_age_days):
            return cur
    try:
        if update_obs:
            # OGIMET serves about two months per request; same span as the Open-Meteo engine's archive
            obs_mod.update(obs_mod.ObsStore(obs_root), stations, days=OBS_DAYS, sess=sess, now=now)
        tab = training_table(archive_root, obs_root, stations,
                             since=now - dt.timedelta(days=WINDOW_DAYS + 40))
        cal = fit(tab, now=now)
        if not cal["temp"]["day"] and not cal["temp"]["night"]:
            raise RuntimeError("no matched forecast/observation pairs yet")
        cal["generated"] = now.isoformat(timespec="seconds")
        scores = backtest(tab, end=pd.Timestamp(now))
        scores["generated"] = cal["generated"]
        try:
            scores["live"] = live_scores(root, obs_root, stations, now=now)
        except Exception as exc:  # noqa: BLE001 — the live log is a report, never a reason to fail
            print(f"[consensus] live scores failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        for path, obj in ((cal_path, cal), (root / "scores.json", scores)):
            tmp = path.with_suffix(".part")
            tmp.write_text(json.dumps(finite(obj), ensure_ascii=False, default=float), encoding="utf-8")
            tmp.replace(path)
        return cal
    except Exception as exc:  # noqa: BLE001 — keep forecasting with the last good calibration
        print(f"[consensus] refresh failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        if cur and cur.get("generated"):
            age = now - dt.datetime.fromisoformat(cur["generated"]).replace(tzinfo=None)
            if age < dt.timedelta(days=7):
                return cur
        return None


def load_scores(root=DEFAULT_ROOT) -> dict[str, Any] | None:
    p = pathlib.Path(root) / "scores.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    except (ValueError, OSError):
        return None
