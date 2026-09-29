"""Observations, multi-model engine, post-processing and verification. Offline."""
from __future__ import annotations

import datetime as dt
import json

import numpy as np
import pytest

from wxgrid import obs, periods, postproc, product, verify
from wxgrid.points import Township
from wxgrid.sources import openmeteo


# ------------------------------------------------------------------ SYNOP decoding

REP_12Z = "AAXX 27121 58730 14/72 /0201 10284 20231 39820 40072 52025 60001 333 10359 20232 3/026 59018 70068 90761 91106=="
REP_06Z = "AAXX 27061 58730 14/71 /1101 10300 20246 39802 40054 57031 60071 72100 333 10347 20232 3/023 59023 70068 90761 91106=="


def test_decode_a_chinese_synop():
    r = obs.decode(REP_12Z)
    assert r["wmo"] == "58730" and r["t"] == 28.4 and r["td"] == 23.1
    assert r["wind_dir"] == 20.0 and r["wind_speed"] == 1.0
    assert r["p6"] == 0.0 and r["tx24"] == 35.9 and r["tn24"] == 23.2 and r["r24"] == 6.8
    assert r["gust"] == 6.0
    r6 = obs.decode(REP_06Z)
    assert r6["p6"] == 7.0 and r6["ww"] == 21


@pytest.mark.parametrize("code,mm", [("000", 0.0), ("007", 7.0), ("990", 0.05), ("993", 0.3)])
def test_rrr_code_table(code, mm):
    assert obs._rrr(code) == pytest.approx(mm)


def test_negative_temperatures_and_missing_groups():
    r = obs.decode("AAXX 15001 58506 14/// ///// 11023 21045 333 11012 21078==")
    assert r["t"] == -2.3 and r["td"] == -4.5 and r["tx24"] == -1.2 and r["tn24"] == -7.8
    assert "wind_speed" not in r and "p6" not in r


def test_ogimet_csv_and_store_merge(tmp_path):
    text = f"58730,2026,09,27,06,00,{REP_06Z}\n58730,2026,09,27,12,00,{REP_12Z}\njunk\n"
    rows = obs.parse_ogimet(text)
    assert [r["time"] for r in rows] == ["2026-09-27T06:00", "2026-09-27T12:00"]
    st = obs.ObsStore(tmp_path)
    assert st.merge("58730", rows) == 2 and st.merge("58730", rows) == 0
    assert st.last_time("58730") == dt.datetime(2026, 9, 27, 12)


# ------------------------------------------------------------------ verification pairs

def _obs_recs():
    """Two dry days with known extremes and one wet daytime at 58730."""
    recs = {}
    for day in (26, 27, 28):
        for h in (0, 3, 6, 9, 12, 18, 21):
            recs[f"2026-09-{day:02d}T{h:02d}:00"] = {"p6": 0.0, "wind_speed": 2.0, "tx24": 34.0, "tn24": 23.0}
    recs["2026-09-27T06:00"]["p6"] = 3.0
    recs["2026-09-27T12:00"]["p6"] = 4.0
    return recs


def _fc_rec(tmax=35.0, rain_hour="2026-09-27T05:00"):
    times = [(dt.datetime(2026, 9, 25) + dt.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(24 * 5)]
    t = [30.0 if 0 <= int(x[11:13]) <= 12 else 24.0 for x in times]
    t[times.index("2026-09-27T06:00")] = tmax
    p = [5.0 if x == rain_hour else 0.0 for x in times]
    rec = {"station": "58730", "time": times}
    for lead in verify.LEADS:
        rec[f"temperature_2m_previous_day{lead}"] = t
        rec[f"precipitation_previous_day{lead}"] = p
        rec[f"wind_speed_10m_previous_day{lead}"] = [2.5] * len(times)
    return rec


def test_periods_match_cma_windows():
    dates = [dt.date(2026, 9, 27)]
    op = verify.obs_periods(_obs_recs(), dates)
    assert op[("2026-09-27", "day")] == {"t": 34.0, "precip": 7.0, "wind": 2.0}
    assert op[("2026-09-27", "night")]["t"] == 23.0 and op[("2026-09-27", "night")]["precip"] == 0.0
    mp = verify.model_periods(_fc_rec(), 1, dates)
    assert mp[("2026-09-27", "day")]["t"] == 35.0 and mp[("2026-09-27", "day")]["precip"] == 5.0
    assert mp[("2026-09-27", "night")]["t"] == 24.0


def test_scores():
    f, o = np.array([33.0, 36.5, 30.0]), np.array([34.0, 34.0, 30.0])
    s = verify.temp_scores(f, o)
    assert s["mae"] == pytest.approx(3.5 / 3) and s["acc2"] == pytest.approx(200 / 3)
    r = verify.rain_scores(np.array([0.0, 0.5, 2.0, 0.0]), np.array([0.0, 0.05, 3.0, 1.0]))
    assert r["pc"] == 50.0 and r["ts"] == pytest.approx(100 / 3) and r["fbias"] == 1.0  # trace counts as dry


def test_online_bias_never_looks_ahead():
    rows = []
    for i in range(20):
        d = (dt.date(2026, 9, 1) + dt.timedelta(days=i)).isoformat()
        err = 0.0 if i < 10 else 3.0
        rows.append({"model": "m", "station": "S", "date": d, "kind": "day", "lead": 1,
                     "f_t": 30.0 + err, "o_t": 30.0, "f_p": 0, "o_p": 0, "f_w": 1, "o_w": 1})
    out = {r["date"]: r["f_t"] for r in verify.online_bias(rows, alpha=0.5)}
    # the jump on 09-11 is unknown to the forecast for 09-11 and 09-12 (issued a day ahead)
    assert out["2026-09-11"] == pytest.approx(33.0) and out["2026-09-12"] == pytest.approx(33.0)
    assert out["2026-09-13"] < 33.0 and out["2026-09-20"] == pytest.approx(30.0, abs=0.05)


# ------------------------------------------------------------------ quantile map + calibration

def test_qmap_removes_drizzle_and_keeps_order():
    rows = [{"f_p": f, "o_p": o} for f, o in
            [(0.3, 0.0)] * 60 + [(1.0, 0.0)] * 20 + [(4.0, 2.0)] * 15 + [(10.0, 15.0)] * 5]
    fit = postproc._qmap_fit(rows)
    m = postproc.qmap(np.array([0.0, 0.3, 1.0, 4.0, 10.0, 40.0]), fit)
    assert m[0] == 0.0 and m[1] == 0.0 and m[2] < 0.1          # drizzle is mapped to dry
    assert m[3] > 0.1 and m[4] == pytest.approx(15.0)          # real rain survives, heavy rain is raised
    # above the training range the top ratio (15 / 10) carries on; raises are capped at MAX_UP
    assert np.all(np.diff(m) >= 0) and m[5] == pytest.approx(40.0 * min(1.5, postproc.MAX_UP))
    assert np.all(postproc.qmap(np.array([2.0, 8.0, 30.0]), fit) <= np.array([2.0, 8.0, 30.0]) * postproc.MAX_UP + 1e-9)


def _period_ds(init="2026-09-28T09:00", precip=1.0):
    import xarray as xr
    steps = np.arange(3, 145, 3)
    base = np.datetime64(init, "s")
    shp = (1, steps.size)
    return xr.Dataset(
        {"t2m": (("point", "step"), np.full(shp, 25.0)), "tmax3": (("point", "step"), np.full(shp, 26.0)),
         "tmin3": (("point", "step"), np.full(shp, 24.0)), "precip": (("point", "step"), np.full(shp, precip)),
         "snow": (("point", "step"), np.zeros(shp)), "cloud": (("point", "step"), np.full(shp, 0.9)),
         "u10": (("point", "step"), np.full(shp, 2.0)), "v10": (("point", "step"), np.zeros(shp)),
         "gust": (("point", "step"), np.full(shp, 5.0))},
        coords={"point": ["A"], "step": steps, "valid_time": ("step", base + steps.astype("timedelta64[h]")),
                "name": ("point", ["甲镇"]), "latitude": ("point", [28.3]), "longitude": ("point", [117.7]),
                "elevation": ("point", [60.0])}, attrs={"init_time": init})


def _cal():
    p = np.linspace(0, 1, 11)
    q = {"p": p.tolist(), "fq": (p * 10).tolist(), "oq": (p * 20).tolist(), "n": 100}
    return {"bias": {"day": {"1": 1.0, "2": 2.0}, "night": {"1": -1.0}}, "qmap": {"day": q, "night": q},
            "alpha": 0.1, "window": ["a", "b"]}


def test_apply_shifts_by_kind_and_lead_and_rescales_rain_per_period():
    issue = dt.datetime(2026, 9, 28, 11, 30)            # 19:30 BJT -> tonight first
    ds = _period_ds()
    plan = periods.plan(np.datetime64(ds.attrs["init_time"]), issue)
    out, fac = postproc.apply(ds, plan, _cal(), issue_utc=issue)
    agg0, agg1 = periods.aggregate(ds, plan), periods.aggregate(out, plan)
    assert agg1["tmin"].values[0, 0] == pytest.approx(agg0["tmin"].values[0, 0] + 1.0)   # tonight: night, lead 1
    assert agg1["tmax"].values[0, 1] == pytest.approx(agg0["tmax"].values[0, 1] - 1.0)   # tomorrow day: lead 1
    assert agg1["tmax"].values[0, 3] == pytest.approx(agg0["tmax"].values[0, 3] - 2.0)   # day after: lead 2
    # 4 mm in a period -> quantile 0.4 of the forecast climate -> 8 mm observed, capped at 1.5x
    assert agg1["precip"].values[0, 0] == pytest.approx(4.0 * postproc.MAX_UP)
    assert fac[0][0] == pytest.approx(postproc.MAX_UP)


def test_no_calibration_is_a_no_op():
    ds = _period_ds()
    out, fac = postproc.apply(ds, [], None, issue_utc=dt.datetime(2026, 9, 28, 11, 30))
    assert out is ds and fac == {}


def test_lead_day():
    assert postproc.lead_day(12.5) == 1 and postproc.lead_day(36) == 2 and postproc.lead_day(130) == 5


# ------------------------------------------------------------------ Open-Meteo members -> product

PTS = [Township("A", "甲镇", 28.30, 117.70, 60.0), Township("B", "乙乡", 28.10, 117.60, 600.0)]


def _raw(n_members=4, hours=24 * 8, start="2026-09-28T00:00"):
    t0 = np.datetime64(start, "h")
    times = [str(t0 + np.timedelta64(h, "h"))[:16] for h in range(hours)]
    loc = (np.arange(hours) + 8) % 24
    members = {}
    for k in range(n_members):
        temp = 25 + 5 * np.sin((loc - 9) / 24 * 2 * np.pi) + k * 0.5
        rain = np.where((loc >= 14) & (loc < 17), 1.0, 0.0)
        members[f"m{k}"] = {
            "temperature_2m": np.vstack([temp, temp - 3.5]),
            "precipitation": np.vstack([rain, rain]), "snowfall": np.zeros((2, hours)),
            "cloud_cover": np.full((2, hours), 60.0), "wind_speed_10m": np.full((2, hours), 3.0),
            "wind_direction_10m": np.full((2, hours), 90.0), "wind_gusts_10m": np.full((2, hours), 8.0),
        }
    return {"time": times, "members": members}


def test_member_windows_and_ensemble_mean():
    raw = _raw()
    steps = np.arange(3, 25, 3)
    mem = openmeteo.member_datasets(raw, PTS, "2026-09-28T00:00", steps)
    m0 = mem["m0"]
    # window (3, 6]: t at hour 6, max over hours 3..6, rain over hours 4..6
    assert m0["t2m"].values[0, 1] == pytest.approx(raw["members"]["m0"]["temperature_2m"][0, 6])
    assert m0["tmax3"].values[0, 1] == pytest.approx(raw["members"]["m0"]["temperature_2m"][0, 3:7].max())
    assert m0["precip"].values[0, 1] == pytest.approx(raw["members"]["m0"]["precipitation"][0, 4:7].sum())
    assert m0["u10"].values[0, 0] == pytest.approx(-3.0) and abs(m0["v10"].values[0, 0]) < 1e-9  # east wind
    ens = openmeteo.ensemble(raw, PTS, "2026-09-28T00:00", steps, members=mem)
    assert ens["t2m"].values[0, 0] == pytest.approx(np.mean([mem[k]["t2m"].values[0, 0] for k in mem]))
    assert ens["wind_dir"].values[0, 0] == pytest.approx(90.0) and ens["members"].values[0, 0] == 4


def test_too_few_members_leaves_a_step_missing():
    raw = _raw(n_members=4)
    for k in ("m1", "m2"):
        raw["members"][k]["temperature_2m"][:, 12:] = np.nan
    ens = openmeteo.ensemble(raw, PTS, "2026-09-28T00:00", np.arange(3, 25, 3))
    assert np.isnan(ens["t2m"].values[0, -1]) and np.isfinite(ens["t2m"].values[0, 0])


def test_multimodel_product_end_to_end(monkeypatch, tmp_path):
    issue = dt.datetime(2026, 9, 28, 11, 30)
    monkeypatch.setattr(openmeteo, "fetch", lambda pts, **kw: _raw(hours=24 * 8))
    monkeypatch.setattr(postproc, "refresh", lambda root, **kw: _cal())
    (tmp_path / "scores.json").write_text(json.dumps({"window": ["x", "y"]}), encoding="utf-8")
    prod, hdoc = product.compute_multimodel(PTS, county="测试县", seat="甲镇", issue_utc=issue, want_pop=False,
                                            sess=object(), hourly=True, verify_root=tmp_path)
    m = prod["meta"]
    assert m["engine"] == "multimodel" and m["run"] == "2026092809" and m["calibration"]["alpha"] == 0.1
    assert prod["periods"][0]["label"] == "今天夜间" and len(prod["periods"]) == 11
    assert prod["verification"] == {"window": ["x", "y"]}
    c = prod["periods"][1]["cells"][0]                 # tomorrow daytime at 甲镇
    assert c["weather"] and "风" in c["wind_text"] and c["temp"] is not None
    assert hdoc["meta"]["n_hours"] > 100
    json.dumps(prod, allow_nan=False)


def test_member_probabilities_from_member_quantile_maps():
    raw = _raw(n_members=4)
    raw["members"]["m3"]["precipitation"][:] = 0.0           # one dry member
    init = "2026-09-28T09:00"
    issue = dt.datetime(2026, 9, 28, 11, 30)
    plan = periods.plan(np.datetime64(init), issue)
    mem = openmeteo.member_datasets(raw, PTS, init, periods.steps_for(plan))
    ident = {"p": [0.0, 1.0], "fq": [0.0, 100.0], "oq": [0.0, 100.0], "n": 100}
    cal = {"qmap_members": {k: {"day": ident, "night": ident} for k in mem}}
    pp, sp = postproc.member_period_pop(mem, plan, cal)
    assert pp.shape == (2, len(plan)) and sp.shape == (2, mem["m0"].sizes["step"])
    day = [k for k, p in enumerate(plan) if p.kind == "day"][0]
    assert pp[0, day] == pytest.approx(75.0)                  # 3 of 4 members rain 14-17 BJT
    night = [k for k, p in enumerate(plan) if p.kind == "night"][0]
    assert pp[0, night] == 0.0
    assert postproc.member_period_pop(mem, plan, {}) == (None, None)


def test_engine_falls_back_to_grib_when_open_meteo_fails(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("unreachable")
    called = {}
    monkeypatch.setattr(product, "compute_multimodel", boom)
    monkeypatch.setattr(product, "choose_run", lambda *a, **k: called.setdefault("grib", True) and (_ for _ in ()).throw(KeyError("stop")))
    with pytest.raises(KeyError):
        product.compute_bundle(PTS, county="x", seat="甲镇", sess=object(), issue_utc=dt.datetime(2026, 9, 28, 11, 30))
    assert called["grib"]
