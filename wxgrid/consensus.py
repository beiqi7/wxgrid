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
* **Precipitation** — 12 h totals of the member mean, quantile-mapped from the
  forecast to the observed climatology of the window, recent days weighted
  more (14-day half-life).
* **Wind** — speed is the mean of the members' speeds times a ratio of
  means per period kind (the models run ~20 % light against these
  stations); direction from the averaged vector.
* **Probability** — logistic regression of "≥0.1 mm observed" on the
  members' quantile-mapped amounts (detail only); GEFS gives the 3-hourly
  detail.

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
RIDGE = 1.0
MIN_ROWS = 30
LOWLAND_M = 500.0
WET_MM = verify.WET_MM
KEYS = ["init", "station", "date", "kind", "lead", "end"]


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

def fit_temperature(tab: pd.DataFrame, *, members=MEMBERS, ridge: float = RIDGE,
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
        R = ridge * len(y) * np.eye(X.shape[1])
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


# ------------------------------------------------------------------ fit / apply

def fit(tab: pd.DataFrame, *, now: dt.datetime | None = None, window_days: int = WINDOW_DAYS) -> dict[str, Any]:
    """Calibration from the rows whose periods ended before ``now``, over ``window_days``."""
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    tr = tab[(tab["end"] <= now) & (tab["end"] > now - dt.timedelta(days=window_days))] if len(tab) else tab
    dates = sorted(tr["date"].unique()) if len(tr) else []
    return {
        "method": "weighted member consensus (weights >= 0, sum 1, ridge to equal) + bias, per kind and lead; "
                  "12 h precipitation quantile map; logistic PoP",
        "members": list(MEMBERS), "window": [dates[0], dates[-1]] if dates else None,
        "n_pairs": int(tr["o_t"].notna().sum()) if len(tr) else 0,
        "lapse": {"day": "std", "night": "local"},
        "temp": fit_temperature(tr), "precip": fit_precip(tr) if len(tr) else {}, "pop": fit_pop(tr) if len(tr) else {},
        "wind": fit_wind(tr) if len(tr) else {},
    }


def predict(cal: dict | None, tab: pd.DataFrame) -> pd.DataFrame:
    """Consensus ``t``, ``p`` and ``pop`` for every row of a member table."""
    out = pd.DataFrame(index=tab.index, columns=["t", "p", "pop", "w"], dtype=float)
    for (kind, lead), g in tab.groupby(["kind", "lead"]):
        out.loc[g.index, "w"] = member_mean_wind(g).values * wind_factor(cal, kind)
        vals = {m: g[f"{m}_t"].values for m in MEMBERS}
        out.loc[g.index, "t"] = combine_temperature((cal or {}).get("temp"), kind, int(lead), vals)
        out.loc[g.index, "p"] = map_precip((cal or {}).get("precip"), kind, member_mean_precip(g).values)
        out.loc[g.index, "pop"] = predict_pop((cal or {}).get("pop"), kind,
                                              {m: g[f"{m}_p"].values for m in MEMBERS}, g["gefs_pop"].values)
    return out


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
        preds.append(pd.DataFrame({"new_t": p["t"], "new_p": p["p"], "pop": p["pop"], "new_w": p["w"],
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
    pop = None
    ok = np.isfinite(P["pop"].values.astype(float)) & pmask
    if ok.sum() >= 50:
        f = P["pop"].values.astype(float)[ok]
        o = (rows["o_p"].values[ok] >= WET_MM).astype(float)
        clim = float((tab.loc[tab["end"] < rows["init"].min(), "o_p"].dropna() >= WET_MM).mean())
        bs, bc = float(((f - o) ** 2).mean()), float(((clim - o) ** 2).mean())
        bins = [(0.0, 0.2), (0.2, 0.5), (0.5, 0.8), (0.8, 1.01)]
        pop = {"n": int(ok.sum()), "brier": bs, "brier_climatology": bc, "bss": 1 - bs / bc if bc > 0 else None,
               "reliability": [{"bin": [a, min(b, 1.0)], "n": int(((f >= a) & (f < b)).sum()),
                                "observed": float(o[(f >= a) & (f < b)].mean()) if ((f >= a) & (f < b)).any() else None}
                               for a, b in bins]}
    first, last = rows["init"].min(), rows["init"].max()
    return {
        "window": [(first + pd.Timedelta(days=1)).date().isoformat(), (last + pd.Timedelta(days=1)).date().isoformat()],
        "days": len(inits), "stations": sorted(rows["station"].unique().tolist()),
        "order": ["new", "mean_bias", "new_raw", "ifs"], "baseline": "ifs",
        "configs": configs, "pop": pop, "pop_label": "3 家模式雨量经逻辑回归校准",
        "notes": "白天最高对比 12 时（UTC）报的 24 小时最高气温，夜间最低对比 00 时报的 24 小时最低气温；"
                 "降水为 12 小时（白天 08—20 时、夜间 20—08 时），≥0.1 mm 为有雨，微量按无雨。"
                 "全部样本外：每次预报只用发布前已结束时段的实况拟合。",
    }


# ------------------------------------------------------------------ daily refresh

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
        for path, obj in ((cal_path, cal), (root / "scores.json", scores)):
            tmp = path.with_suffix(".part")
            tmp.write_text(json.dumps(obj, ensure_ascii=False, default=float), encoding="utf-8")
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
