"""Forecast verification against SYNOP stations, the way CMA scores town forecasts.

Forecasts come from Open-Meteo's *Previous Runs* archive, which stores what
each global model predicted at fixed 1–7 day lead times, per point. That makes
it possible to score a dozen models over the same stations and days without
downloading any GRIB — the question "which model is best here" becomes cheap.

Periods and scores follow the CMA town-forecast rules:

* 白天 = 08–20 BJT (00–12 UTC), 夜间 = 20–08 BJT; 白天最高 vs the 24 h maximum
  in the 12 UTC report, 夜间最低 vs the 24 h minimum in the 00 UTC report.
* temperature: MAE, bias, and **准确率** = share of |error| <= 2 ℃.
* 12 h precipitation: **晴雨准确率** (PC), TS / POD / FAR for >= 0.1 mm and
  >= 5 mm (中雨 in the 12 h table), and the frequency bias.
* wind: 10 m speed at the report hours, MAE and bias.

Online post-processing is scored honestly: a correction used on day *d* is
learned only from days that ended before the forecast was issued.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import time
from collections import defaultdict
from typing import Iterable

import numpy as np

from . import obs as obs_mod
from .sources._fetch import get, session

OM_PREVIOUS = "https://previous-runs-api.open-meteo.com/v1/forecast"

#: Open-Meteo model id -> display name. All are global and cover the county.
MODELS = {
    "ecmwf_ifs": "ECMWF IFS 9 km",
    "ecmwf_ifs025": "ECMWF IFS 0.25°",
    "ecmwf_aifs025_single": "ECMWF AIFS（AI）",
    "gfs_global": "NOAA GFS",
    "ncep_aigfs025": "NOAA AIGFS（AI）",
    "cma_grapes_global": "CMA GRAPES",
    "icon_global": "DWD ICON",
    "jma_gsm": "JMA GSM",
    "ukmo_global_deterministic_10km": "UKMO 10 km",
    "gem_global": "CMC GEM",
    "meteofrance_arpege_world": "Météo-France ARPEGE",
}
LEADS = (1, 2, 3, 4, 5)
VARS = ("temperature_2m", "precipitation", "wind_speed_10m")
WET_MM = 0.1
MODERATE_12H_MM = 5.0


# ------------------------------------------------------------------ fetch

def fetch_model(model: str, stations: list[obs_mod.Station], start: dt.date, end: dt.date, *,
                cache_dir: pathlib.Path, sess=None, pause: float = 2.0) -> list[dict]:
    """Hourly previous-run series for every station; cached on disk per (model, window)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{model}_{start:%Y%m%d}_{end:%Y%m%d}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    sess = sess or session()
    hourly = ",".join(f"{v}_previous_day{n}" for v in VARS for n in LEADS)
    q = (f"{OM_PREVIOUS}?latitude={','.join(str(s.lat) for s in stations)}"
         f"&longitude={','.join(str(s.lon) for s in stations)}"
         f"&elevation={','.join(str(s.elevation_m) for s in stations)}"
         f"&hourly={hourly}&models={model}&start_date={start}&end_date={end}"
         f"&timezone=GMT&wind_speed_unit=ms")
    raw = json.loads(get(sess, q, timeout=120, retries=4).decode("utf-8"))
    if isinstance(raw, dict):
        if raw.get("error"):
            raise RuntimeError(f"{model}: {raw.get('reason')}")
        raw = [raw]
    out = [{"station": s.wmo, "time": r["hourly"]["time"],
            **{k: v for k, v in r["hourly"].items() if k != "time"}} for s, r in zip(stations, raw)]
    path.write_text(json.dumps(out), encoding="utf-8")
    time.sleep(pause)
    return out


# ------------------------------------------------------------------ periods

def _idx(times: list[str]) -> dict[str, int]:
    return {t: i for i, t in enumerate(times)}


def _vals(series: list, idx: dict[str, int], stamps: list[str]):
    out = []
    for s in stamps:
        j = idx.get(s)
        v = series[j] if j is not None else None
        if v is None:
            return None
        out.append(float(v))
    return out


def _stamp(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M")


def model_periods(rec: dict, lead: int, dates: list[dt.date]) -> dict[tuple[str, str], dict]:
    """``{(date, 'day'|'night'): {t, precip, wind}}`` for one station/model/lead.

    ``night`` of key date D is the night *ending* on D (20 BJT D-1 to 08 BJT D),
    matching the 00 UTC report's 24 h minimum.
    """
    idx = _idx(rec["time"])
    tk, pk, wk = (f"{v}_previous_day{lead}" for v in VARS)
    T, P, W = rec.get(tk), rec.get(pk), rec.get(wk)
    if T is None:
        return {}
    out = {}
    for d in dates:
        base = dt.datetime(d.year, d.month, d.day)
        day_t = [_stamp(base + dt.timedelta(hours=h)) for h in range(0, 13)]
        day_p = [_stamp(base + dt.timedelta(hours=h)) for h in range(1, 13)]
        day_w = [_stamp(base + dt.timedelta(hours=h)) for h in (3, 6, 9, 12)]
        ngt_t = [_stamp(base + dt.timedelta(hours=h)) for h in range(-12, 1)]
        ngt_p = [_stamp(base + dt.timedelta(hours=h)) for h in range(-11, 1)]
        ngt_w = [_stamp(base + dt.timedelta(hours=h)) for h in (-6, -3, 0)]
        for kind, ts, ps, ws, f in (("day", day_t, day_p, day_w, max), ("night", ngt_t, ngt_p, ngt_w, min)):
            t = _vals(T, idx, ts)
            p = _vals(P, idx, ps) if P is not None else None
            w = _vals(W, idx, ws) if W is not None else None
            if t is None:
                continue
            out[(d.isoformat(), kind)] = {"t": f(t), "precip": None if p is None else sum(p),
                                          "wind": None if w is None else max(w)}
    return out


def obs_periods(recs: dict[str, dict], dates: list[dt.date]) -> dict[tuple[str, str], dict]:
    """Observed 白天最高 / 夜间最低, 12 h precipitation and max 10-min wind, same keys."""
    out = {}
    for d in dates:
        t12 = dt.datetime(d.year, d.month, d.day, 12)
        t00 = dt.datetime(d.year, d.month, d.day, 0)

        def at(t, k):
            r = recs.get(_stamp(t))
            return None if r is None else r.get(k)

        def p6sum(ends):
            v = [at(t, "p6") for t in ends]
            return None if any(x is None for x in v) else float(sum(v))

        def wmax(ts):
            v = [at(t, "wind_speed") for t in ts]
            v = [x for x in v if x is not None]
            return max(v) if len(v) >= len(ts) - 1 else None

        out[(d.isoformat(), "day")] = {
            "t": at(t12, "tx24"), "precip": p6sum([t12 - dt.timedelta(hours=6), t12]),
            "wind": wmax([t00 + dt.timedelta(hours=h) for h in (3, 6, 9, 12)])}
        out[(d.isoformat(), "night")] = {
            "t": at(t00, "tn24"), "precip": p6sum([t00 - dt.timedelta(hours=6), t00]),
            "wind": wmax([t00 + dt.timedelta(hours=h) for h in (-6, -3, 0)])}
    return out


# ------------------------------------------------------------------ scores

def temp_scores(f: np.ndarray, o: np.ndarray) -> dict:
    e = f - o
    return {"n": int(e.size), "mae": float(np.abs(e).mean()), "bias": float(e.mean()),
            "acc2": float((np.abs(e) <= 2.0).mean() * 100)} if e.size else {"n": 0}


def rain_scores(f: np.ndarray, o: np.ndarray, *, f_thr: float = WET_MM, o_thr: float = WET_MM) -> dict:
    """Dichotomous scores. Trace observations (0.05) count as dry, as in CMA 晴雨 scoring."""
    fy, oy = f >= f_thr, o >= o_thr
    hit = int((fy & oy).sum())
    miss = int((~fy & oy).sum())
    fa = int((fy & ~oy).sum())
    cn = int((~fy & ~oy).sum())
    n = hit + miss + fa + cn
    return {"n": n, "pc": (hit + cn) / n * 100 if n else float("nan"),
            "ts": hit / (hit + miss + fa) * 100 if hit + miss + fa else float("nan"),
            "pod": hit / (hit + miss) * 100 if hit + miss else float("nan"),
            "far": fa / (hit + fa) * 100 if hit + fa else float("nan"),
            "fbias": (hit + fa) / (hit + miss) if hit + miss else float("nan"),
            "obs_freq": (hit + miss) / n * 100 if n else float("nan")}


def wind_scores(f: np.ndarray, o: np.ndarray) -> dict:
    e = f - o
    return {"n": int(e.size), "mae": float(np.abs(e).mean()), "bias": float(e.mean())} if e.size else {"n": 0}


# ------------------------------------------------------------------ assembling pairs

class Matched:
    """Aligned forecast/observation pairs: one row per (station, date, kind, lead)."""

    def __init__(self):
        self.rows: list[dict] = []

    @classmethod
    def build(cls, fc: dict[str, list[dict]], observed: dict[str, dict], dates: list[dt.date],
              leads: Iterable[int] = LEADS) -> "Matched":
        m = cls()
        obs_p = {w: obs_periods(r, dates) for w, r in observed.items()}
        for model, recs in fc.items():
            for rec in recs:
                op = obs_p.get(rec["station"], {})
                for lead in leads:
                    for key, v in model_periods(rec, lead, dates).items():
                        o = op.get(key)
                        if not o:
                            continue
                        m.rows.append({"model": model, "station": rec["station"], "date": key[0],
                                       "kind": key[1], "lead": lead, "f_t": v["t"], "o_t": o["t"],
                                       "f_p": v["precip"], "o_p": o["precip"], "f_w": v["wind"], "o_w": o["wind"]})
        return m

    def select(self, **kw) -> list[dict]:
        return [r for r in self.rows if all(r[k] == v if not callable(v) else v(r[k]) for k, v in kw.items())]

    @staticmethod
    def arrays(rows: list[dict], a: str, b: str) -> tuple[np.ndarray, np.ndarray]:
        pair = [(r[a], r[b]) for r in rows if r[a] is not None and r[b] is not None]
        if not pair:
            return np.array([]), np.array([])
        x = np.array(pair, dtype=float)
        return x[:, 0], x[:, 1]


def score_table(m: Matched, *, stations: set[str] | None = None, models: Iterable[str] | None = None,
                leads: Iterable[int] = LEADS) -> dict:
    """``{model: {lead: {tmax, tmin, rain, rain5, wind}}}`` over the chosen stations."""
    out: dict = {}
    keep = (lambda s: s in stations) if stations else (lambda s: True)
    by = defaultdict(list)
    for r in m.rows:
        if keep(r["station"]):
            by[(r["model"], r["lead"])].append(r)
    for model in models or sorted({r["model"] for r in m.rows}):
        out[model] = {}
        for lead in leads:
            rows = by.get((model, lead), [])
            day = [r for r in rows if r["kind"] == "day"]
            ngt = [r for r in rows if r["kind"] == "night"]
            fp, op = Matched.arrays(rows, "f_p", "o_p")
            fw, ow = Matched.arrays(rows, "f_w", "o_w")
            out[model][lead] = {
                "tmax": temp_scores(*Matched.arrays(day, "f_t", "o_t")),
                "tmin": temp_scores(*Matched.arrays(ngt, "f_t", "o_t")),
                "rain": rain_scores(fp, op),
                "rain5": rain_scores(fp, op, f_thr=MODERATE_12H_MM, o_thr=MODERATE_12H_MM),
                "wind": wind_scores(fw, ow),
            }
    return out


# ------------------------------------------------------------------ blending + online correction

def blend_rows(m: Matched, members: dict[str, float], name: str) -> list[dict]:
    """Weighted mean of member forecasts, only where every member has a value."""
    groups = defaultdict(dict)
    for r in m.rows:
        if r["model"] in members:
            groups[(r["station"], r["date"], r["kind"], r["lead"])][r["model"]] = r
    out = []
    wsum = sum(members.values())
    for key, rs in groups.items():
        if len(rs) != len(members):
            continue
        any_r = next(iter(rs.values()))
        row = {"model": name, "station": key[0], "date": key[1], "kind": key[2], "lead": key[3],
               "o_t": any_r["o_t"], "o_p": any_r["o_p"], "o_w": any_r["o_w"]}
        for f in ("f_t", "f_p", "f_w"):
            vals = [(rs[k][f], w) for k, w in members.items()]
            row[f] = None if any(v is None for v, _ in vals) else sum(v * w for v, w in vals) / wsum
        out.append(row)
    return out


def online_bias(rows: list[dict], *, alpha: float = 0.15, pooled: bool = True) -> list[dict]:
    """Decaying-average temperature bias correction, applied without look-ahead.

    For each (kind, lead) the bias is an exponentially weighted mean of past daily
    errors (pooled over stations when ``pooled``). A forecast for date d at lead L
    was issued about L days earlier, so it may only use errors from dates <= d - L - 1.
    """
    out = []
    by_kl = defaultdict(list)
    for r in rows:
        by_kl[(r["kind"], r["lead"], None if pooled else r["station"])].append(r)
    for (kind, lead, _st), rs in by_kl.items():
        dates = sorted({r["date"] for r in rs})
        daily_err = {}
        for d in dates:
            e = [r["f_t"] - r["o_t"] for r in rs if r["date"] == d and r["f_t"] is not None and r["o_t"] is not None]
            if e:
                daily_err[d] = float(np.mean(e))
        bias, have = 0.0, False
        state = {}
        for d in dates:  # state[d] = bias known after the errors of day d
            if d in daily_err:
                bias = daily_err[d] if not have else (1 - alpha) * bias + alpha * daily_err[d]
                have = True
            state[d] = bias if have else None
        known = sorted(state)
        for r in rs:
            cutoff = (dt.date.fromisoformat(r["date"]) - dt.timedelta(days=lead + 1)).isoformat()
            prior = [d for d in known if d <= cutoff and state[d] is not None]
            b = state[prior[-1]] if prior else 0.0
            nr = dict(r)
            if nr["f_t"] is not None:
                nr["f_t"] = nr["f_t"] - b
            out.append(nr)
    return out


def best_threshold(rows: list[dict], *, grid=(0.1, 0.2, 0.3, 0.5, 0.7, 1.0, 1.5, 2.0, 3.0)) -> tuple[float, dict]:
    """The model-precipitation cut (mm / 12 h) that maximises 晴雨准确率 on ``rows``."""
    f, o = Matched.arrays(rows, "f_p", "o_p")
    best = max(grid, key=lambda t: rain_scores(f, o, f_thr=t)["pc"])
    return best, rain_scores(f, o, f_thr=best)


def inverse_mse_weights(m: Matched, models: list[str], field: str = "f_t", obs: str = "o_t",
                        dates: set[str] | None = None) -> dict[str, float]:
    w = {}
    for model in models:
        rows = [r for r in m.rows if r["model"] == model and (dates is None or r["date"] in dates)]
        f, o = Matched.arrays(rows, field, obs)
        mse = float(((f - o) ** 2).mean()) if f.size else float("inf")
        w[model] = 1.0 / mse if mse > 0 else 0.0
    s = sum(w.values())
    return {k: v / s for k, v in w.items()}
