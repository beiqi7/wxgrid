"""Native consensus: fitting, combining, backtest, and the live engine end to end. Offline."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from wxgrid import archive, consensus, native
from wxgrid.gribbox import BoxRun
from wxgrid.points import Township


def _table(days=80, seed=1):
    """Synthetic member table: IFS warm by 1 K and good, AIFS cold by 0.5 K and fair, GFS unbiased but noisy."""
    rng = np.random.default_rng(seed)
    rows = []
    t0 = dt.datetime(2025, 5, 1, 12)
    for d in range(days):
        init = t0 + dt.timedelta(days=d)
        for st in ("A", "B", "C", "D", "E"):
            for lead in (1, 2, 3):
                for kind in ("day", "night"):
                    end = init + dt.timedelta(hours=24 * lead + (0 if kind == "day" else 12))
                    o = 25.0 + 5 * np.sin(d / 9.0) + (6 if kind == "day" else -2) + rng.normal(0, 1.0)
                    wet = rng.random() < 0.4
                    o_p = rng.gamma(1.0, 6.0) if wet else 0.0
                    drizzle = lambda: rng.gamma(0.6, 0.6)  # noqa: E731
                    rows.append({
                        "init": init, "station": st, "date": end.date().isoformat(), "kind": kind, "lead": lead,
                        "end": end, "o_t": o, "o_p": o_p, "o_w": 2.0,
                        "ifs_t": o + 1.0 + rng.normal(0, 0.6), "aifs_t": o - 0.5 + rng.normal(0, 1.0),
                        "gfs_t": o + rng.normal(0, 2.0),
                        "ifs_p": (o_p * 0.8 if wet else 0.0) + drizzle(), "aifs_p": (o_p if wet else 0.0) + drizzle(),
                        "gfs_p": (o_p * 1.2 if wet else 0.0) + drizzle(),
                        "gefs_pop": np.clip((0.75 if wet else 0.2) + rng.normal(0, 0.15), 0, 1),
                        "gefs_pens": 1.0})
    tab = pd.DataFrame(rows)
    for m in consensus.MEMBERS:
        tab[f"{m}_t_std"] = tab[f"{m}_t"]
        tab[f"{m}_w"] = 2.0
    tab["init"] = pd.to_datetime(tab["init"])
    tab["end"] = pd.to_datetime(tab["end"])
    return tab


def test_fit_weights_are_a_convex_combination_and_remove_the_bias():
    tab = _table()
    cal = consensus.fit(tab, now=dt.datetime(2025, 8, 1))
    e = cal["temp"]["day"]["1"]
    w = e["w"]
    assert abs(sum(w.values()) - 1) < 1e-3 and min(w.values()) >= 0
    assert w["ifs"] > w["aifs"] > w["gfs"]                     # by error variance
    pred = consensus.predict(cal, tab)
    err = (pred["t"] - tab["o_t"]).abs().mean()
    assert abs((pred["t"] - tab["o_t"]).mean()) < 0.1          # bias removed
    for m in consensus.MEMBERS:
        assert err < (tab[f"{m}_t"] - tab["o_t"]).abs().mean()


def test_weights_sum_to_one_keeps_the_elevation_signal():
    """A township 300 m higher stays ~2 ℃ cooler after combining, whatever the weights."""
    cal = {"temp": {"day": {"1": {"w": {"ifs": 0.7, "aifs": 0.3, "gfs": 0.0}, "a": -0.4, "n": 100}}}}
    low = consensus.combine_temperature(cal["temp"], "day", 1, {m: np.array([30.0]) for m in consensus.MEMBERS})
    high = consensus.combine_temperature(cal["temp"], "day", 1, {m: np.array([28.05]) for m in consensus.MEMBERS})
    assert low - high == pytest.approx(1.95)


def test_missing_member_renormalises():
    cal = {"temp": {"night": {"2": {"w": {"ifs": 0.5, "aifs": 0.3, "gfs": 0.2}, "a": 0.0, "n": 100}}}}
    vals = {"ifs": np.array([10.0, np.nan]), "aifs": np.array([12.0, 12.0]), "gfs": np.array([np.nan, 14.0])}
    out = consensus.combine_temperature(cal["temp"], "night", 2, vals)
    assert out[0] == pytest.approx((0.5 * 10 + 0.3 * 12) / 0.8)
    assert out[1] == pytest.approx((0.3 * 12 + 0.2 * 14) / 0.5)
    # no fit at all: the plain mean
    assert consensus.combine_temperature(None, "day", 1, {"a": np.array([1.0]), "b": np.array([3.0])})[0] == 2.0


def test_precip_quantile_map_removes_drizzle():
    tab = _table()
    cal = consensus.fit(tab, now=dt.datetime(2025, 8, 1))
    pred = consensus.predict(cal, tab)
    dry = tab["o_p"] < 0.1
    raw_mean = consensus.member_mean_precip(tab)
    assert (raw_mean[dry] >= 0.1).mean() > 0.5                 # averaging drizzles
    assert (pred["p"][dry] >= 0.1).mean() < (raw_mean[dry] >= 0.1).mean() / 2


def test_pop_is_calibrated_and_falls_back_to_gefs():
    tab = _table()
    cal = consensus.fit(tab, now=dt.datetime(2025, 8, 1))
    pred = consensus.predict(cal, tab)
    o = (tab["o_p"] >= 0.1).astype(float)
    assert ((pred["pop"] - o) ** 2).mean() < ((tab["gefs_pop"] - o) ** 2).mean()
    share = np.array([0.3, 0.9])
    totals = {"ifs": np.array([0.0, 2.0]), "aifs": np.array([0.0, 0.0]), "gfs": np.array([0.5, 3.0])}
    np.testing.assert_allclose(consensus.predict_pop(None, "day", totals, share), share)      # GEFS fallback
    np.testing.assert_allclose(consensus.predict_pop(None, "day", totals), [1 / 3, 2 / 3])    # member share


def test_backtest_is_out_of_sample_and_beats_the_raw_mean():
    tab = _table()
    s = consensus.backtest(tab, test_days=25)
    assert s["days"] == 25 and s["order"][0] == "new"
    new, raw = s["configs"]["new"]["scores"], s["configs"]["new_raw"]["scores"]
    assert new[1]["tmax"]["mae"] < raw[1]["tmax"]["mae"]
    assert new[1]["rain"]["pc"] > raw[1]["rain"]["pc"]
    assert s["pop"]["bss"] > 0.2
    # a forecast never sees its own observation: shuffling future observations changes nothing
    cut = tab["init"].max() - pd.Timedelta(days=3)
    tab2 = tab.copy()
    tab2.loc[tab2["end"] > cut + pd.Timedelta(days=4), "o_t"] += 50.0
    s2 = consensus.backtest(tab2, test_days=25, end=cut)
    s1 = consensus.backtest(tab, test_days=25, end=cut)
    assert s1["configs"]["new"]["scores"][1]["tmax"] == s2["configs"]["new"]["scores"][1]["tmax"]


def test_backtest_needs_history():
    assert consensus.backtest(_table(days=8), test_days=30)["error"]


#: Observed temperature at each BJT hour relative to its period's extreme (白天 below the maximum, 夜间 above the minimum).
OBS_SHAPE = {11: -3.0, 14: -0.5, 17: -1.0, 20: -4.0, 23: 5.0, 2: 3.0, 5: 1.0, 8: 0.5}


def _series(tab, model_shape=OBS_SHAPE, seed=5):
    """3-hourly rows for every period of ``tab``: observed and member values are the period's
    extreme plus a diurnal shape (the members' ``model_shape`` may miss the observed one)."""
    rng = np.random.default_rng(seed)
    cols = [*consensus.KEYS, "o_t", *[f"{m}_{c}" for m in consensus.MEMBERS for c in ("t", "w")]]
    parts = []
    for back in (9, 6, 3, 0):
        f = tab[cols].copy()
        f["valid"] = f["end"] - pd.Timedelta(hours=back)
        f["hour"] = (f["valid"] + pd.Timedelta(hours=8)).dt.hour
        parts.append(f)
    s = pd.concat(parts, ignore_index=True)
    s["o_t3"] = s["o_t"] + s["hour"].map(OBS_SHAPE).values + rng.normal(0, 0.5, len(s))
    for m in consensus.MEMBERS:
        s[f"{m}_t3"] = s[f"{m}_t"] + s["hour"].map(model_shape).values + rng.normal(0, 0.5, len(s))
    return s.drop(columns=["o_t", *[f"{m}_t" for m in consensus.MEMBERS]]).sort_values(
        ["init", "station", "valid"]).reset_index(drop=True)


def _mae(sc, lead, hour=None):
    s = sc[lead]["all"] if hour is None else sc[lead]["by_hour"][hour]
    return s["mae"]


def test_3_hourly_curve_is_scored_by_hour_and_lead_out_of_sample():
    tab = _table()
    t3 = consensus.backtest(tab, test_days=25, series=_series(tab))["temp_3h"]
    assert t3["order"] == ["current", "hour_bias"] and t3["in_use"] == "current"
    assert t3["hours"] == ["02", "05", "08", "11", "14", "17", "20", "23"]
    cur, hb = (t3["configs"][k]["scores"] for k in t3["order"])
    assert set(cur) == {1, 2, 3, 4, 5} and set(cur[1]["by_hour"]) == set(t3["hours"])
    assert cur[1]["all"]["n"] == hb[1]["all"]["n"] == 25 * 5 * 8          # issues x stations x hours
    assert all(cur[ld]["all"]["n"] == 0 for ld in (4, 5))                 # the synthetic table stops at day 3
    # the members get the diurnal shape right: the extremes' bias holds at every hour, a bias per hour costs ~nothing
    for ld in (1, 2, 3):
        assert abs(_mae(hb, ld) - _mae(cur, ld)) < 0.03
        assert all(abs(s["bias"]) < 0.35 for s in cur[ld]["by_hour"].values())
    # a forecast never sees its own observation
    s3 = _series(tab)
    cut = tab["init"].max() - pd.Timedelta(days=3)
    late = s3.assign(o_t3=np.where(s3["end"] > cut + pd.Timedelta(days=4), s3["o_t3"] + 50.0, s3["o_t3"]))
    a = consensus.backtest(tab, test_days=25, end=cut, series=s3)["temp_3h"]["configs"]
    b = consensus.backtest(tab, test_days=25, end=cut, series=late)["temp_3h"]["configs"]
    for k in ("current", "hour_bias"):
        assert a[k]["scores"][1] == b[k]["scores"][1]
    assert consensus.backtest(tab, test_days=25)["temp_3h"] is None       # no 3-hourly table: no key filled


def test_hour_bias_fixes_a_diurnal_error_the_extremes_cannot_see():
    """Members too warm in the evening and too flat in the morning, extremes right: the extremes'
    bias leaves those hours wrong, the per-hour bias removes it."""
    tab = _table()
    shape = {**OBS_SHAPE, 11: -2.0, 20: -3.0, 23: 6.5, 2: 3.8}
    t3 = consensus.backtest(tab, test_days=25, series=_series(tab, shape))["temp_3h"]
    cur, hb = (t3["configs"][k]["scores"] for k in ("current", "hour_bias"))
    for ld in (1, 2, 3):
        assert _mae(hb, ld) < 0.8 * _mae(cur, ld)
        for h, e in {"11": 1.0, "20": 1.0, "23": 1.5, "02": 0.8}.items():
            assert cur[ld]["by_hour"][h]["bias"] == pytest.approx(e, abs=0.3)
            assert abs(hb[ld]["by_hour"][h]["bias"]) < 0.3
        assert _mae(hb, ld, "14") <= _mae(cur, ld, "14") + 0.05            # hours that were right stay right
    cal = consensus.fit(tab, now=dt.datetime(2025, 8, 1), series=_series(tab, shape))
    e = cal["temp_3h"]["1"]
    assert set(e) == {"02", "05", "08", "11", "14", "17", "20", "23"} and e["23"]["n"] >= consensus.MIN_ROWS
    assert consensus.hour_bias_delta(cal, "night", 1, 23) == pytest.approx(-1.5, abs=0.2)
    assert consensus.hour_bias_delta(cal, "day", 1, 14) == pytest.approx(0.0, abs=0.2)
    assert consensus.hour_bias_delta(cal, "day", 4, 14) == 0.0                # unfitted lead: the extremes' bias
    np.testing.assert_allclose(consensus.hold_to_extreme([31.0, 29.0, 11.0, 13.0], ["day", "day", "night", "night"],
                                                         [30.0, 30.0, 12.0, np.nan]), [30.0, 29.0, 12.0, 13.0])


# ------------------------------------------------------------------ live engine, network replaced

PTS = [Township("T1", "甲镇", 28.30, 117.70, 60.0), Township("T2", "乙乡", 27.90, 117.60, 600.0)]


def _run(source, init, steps, lat, lon, *, cadence):
    steps = np.array(list(steps))
    valid_h = np.array([(init + dt.timedelta(hours=int(s))).hour for s in steps])
    t = 22.0 + 5.0 * np.cos((valid_h - 6) / 24.0 * 2 * np.pi)
    shp = (steps.size, lat.size, lon.size)
    f = {"t2m": np.broadcast_to(t[:, None, None], shp).copy(), "u10": np.full(shp, 2.0), "v10": np.full(shp, 1.0),
         "tp": np.broadcast_to(np.cumsum(np.where(steps > 30, 0.8, 0.0))[:, None, None], shp).copy(),
         "tcc": np.full(shp, 0.5)}
    if cadence == 3:
        f["tmax"], f["tmin"], f["gust"] = f["t2m"] + 0.4, f["t2m"] - 0.4, np.full(shp, 6.0)
    return BoxRun(source=source, init=init, steps=steps, lat=lat, lon=lon, fields=f,
                  orog=np.full((lat.size, lon.size), 200.0), window_start=steps - cadence, meta={"cadence": cadence})


def test_native_compute_end_to_end(tmp_path, monkeypatch):
    from wxgrid.sources import raw

    def fake_fetch(model, init, steps, box, **kw):
        lat = np.arange(box.lat_max, box.lat_min - 0.01, -0.25)[: int((box.lat_max - box.lat_min) / 0.25) + 1]
        lon = np.arange(box.lon_min, box.lon_max + 0.01, 0.25)
        return _run(model.key, init, steps, lat, lon, cadence=model.cadence)

    def fake_gefs(init, steps, box, **kw):
        lat = np.arange(box.lat_max, box.lat_min - 0.01, -0.5)
        lon = np.arange(box.lon_min, box.lon_max + 0.01, 0.5)
        steps = np.array(list(steps))
        f = {f"tp_m{k:02d}": np.broadcast_to((np.arange(steps.size) * (0.3 if k % 2 else 0.0))[:, None, None],
                                             (steps.size, lat.size, lon.size)).copy() for k in range(21)}
        return BoxRun(source="gefs", init=init, steps=steps, lat=lat, lon=lon, fields=f)

    monkeypatch.setattr(raw, "probe", lambda *a, **k: True)
    monkeypatch.setattr(raw, "fetch", fake_fetch)
    monkeypatch.setattr(raw, "fetch_gefs", fake_gefs)
    issue = dt.datetime(2026, 10, 7, 23, 30)
    prod, hdoc = native.compute(PTS, county="测试县", seat="甲镇", issue_utc=issue, hourly=True,
                                calibrate=False, data_dir=tmp_path, stations=[])
    m = prod["meta"]
    assert m["engine"] == "native" and m["cycle"] == "2026100712" and m["sources"] == ["ifs", "aifs", "gfs"]
    assert m["n_periods"] == 10 and prod["periods"][0]["label"] == "今天白天"
    assert m["pop_source"] == "native" and m["pop_members"] == 21
    assert "Open-Meteo" not in m["attribution"]
    hi = {c["point"]: c["temp"] for c in prod["periods"][0]["cells"]}
    assert hi["T1"] - hi["T2"] == pytest.approx(0.0065 * 540, abs=0.05)   # daytime: standard lapse rate
    pops = [c["pop"] for q in prod["periods"] for c in q["cells"]]
    assert all(p is not None and 0 <= p <= 100 for p in pops)
    # uncalibrated: no 中雨 / 大雨以上 probabilities, and the page is told why
    assert all(c["pop_moderate"] is None and c["pop_heavy"] is None for q in prod["periods"] for c in q["cells"])
    assert prod["periods"][0]["county"]["pop_moderate_max"] is None
    assert any("订正未生效" in h for h in m["health"])
    assert prod["series3h"]["points"]["T1"]["temp"][0] is not None
    assert hdoc["meta"]["n_hours"] > 100
    for src in ("ifs", "aifs", "gfs", "gefs"):
        assert archive.inits(tmp_path / "archive", src) == [dt.datetime(2026, 10, 7, 12)]
    assert "ECMWF IFS 0.25°" in prod["text"]


def test_native_compute_needs_two_members(tmp_path, monkeypatch):
    from wxgrid.sources import raw
    monkeypatch.setattr(raw, "probe", lambda model, *a, **k: model.key == "gfs")
    with pytest.raises(RuntimeError):
        native.compute(PTS, county="测试县", seat="甲镇", issue_utc=dt.datetime(2026, 10, 7, 23, 30),
                       calibrate=False, data_dir=tmp_path, stations=[])


def test_choose_cycle_skips_cycles_that_cannot_cover_the_forecast(monkeypatch):
    from wxgrid.sources import raw
    seen = []

    def probe(model, cycle, step, **kw):
        seen.append((model.key, cycle, step))
        return cycle.hour == 0                          # the 12 UTC cycle is not out yet
    monkeypatch.setattr(raw, "probe", probe)
    issue = dt.datetime(2026, 10, 7, 23, 30)
    lead = {dt.datetime(2026, 10, 7, 12): 132, dt.datetime(2026, 10, 7, 0): 144}
    cycle, have = native.choose_cycle(issue, lambda c: lead.get(c))
    assert cycle == dt.datetime(2026, 10, 7, 0) and have == ["ifs", "aifs", "gfs"]
    assert ("aifs", dt.datetime(2026, 10, 7, 12), 132) in seen      # AIFS rounded up to its 6 h step
    with pytest.raises(RuntimeError):
        native.choose_cycle(issue, lambda c: None)


# ------------------------------------------------------------------ night wind, heavy rain, live log, health

def _windy_table(k=1.2, seed=3):
    """Nights colder than the members by ``k`` K per unit of ln(wind) below the mean (calm nights cool more)."""
    tab = _table(seed=seed)
    rng = np.random.default_rng(seed)
    wind = np.exp(rng.normal(0.7, 0.6, len(tab)))
    for m in consensus.MEMBERS:
        tab[f"{m}_w"] = wind * rng.uniform(0.9, 1.1, len(tab))
    x = np.log(np.clip(wind, *consensus.NIGHT_WIND_MS))
    night = tab["kind"] == "night"
    tab.loc[night, "o_t"] += k * (x[night.values] - x[night.values].mean())
    return tab


def test_night_wind_term_moves_calm_and_windy_nights_apart():
    tab = _windy_table()
    cal = consensus.fit(tab, now=dt.datetime(2025, 8, 1))
    assert cal["night_wind"]["b"] == pytest.approx(1.2, abs=0.25)
    with_term = consensus.predict(cal, tab)["t"]
    without = consensus.consensus_t(cal, tab, night_wind=False)
    night = (tab["kind"] == "night").values
    err = lambda f: np.abs(np.asarray(f)[night] - tab["o_t"].values[night]).mean()  # noqa: E731
    assert err(with_term) < err(without) - 0.1
    # days untouched, and the term is centred: it does not shift the average night
    np.testing.assert_allclose(np.asarray(with_term)[~night], without[~night])
    assert abs((np.asarray(with_term) - without)[night].mean()) < 0.1
    # unknown wind or no fit -> no adjustment
    np.testing.assert_allclose(consensus.night_wind_adjust(cal["night_wind"], [np.nan]), [0.0])
    np.testing.assert_allclose(consensus.night_wind_adjust(None, [1.0, 5.0]), [0.0, 0.0])


def test_heavy_rain_probabilities_are_skilful_and_ordered():
    tab = _table()
    cal = consensus.fit(tab, now=dt.datetime(2025, 8, 1))
    h = cal["heavy"]
    assert 5.0 in h["published"] and len(h["coef"]) == 1 + 4 + 2
    pred = consensus.predict(cal, tab)
    assert (pred["p15"] <= pred["p5"] + 1e-12).all() and (pred["p5"] <= pred["pop"] + 1e-12).all()
    o5 = (tab["o_p"] >= 5).astype(float)
    assert ((pred["p5"] - o5) ** 2).mean() < ((o5.mean() - o5) ** 2).mean() * 0.8
    s = consensus.backtest(tab, test_days=25)
    assert s["heavy"]["5"]["bss"] > 0.1 and set(s["heavy"]["5"]["bss_by_lead"]) == {"1", "2", "3", "4", "5"}
    # too few events in the window: nothing published
    dry = tab.assign(o_p=np.where(tab["o_p"] >= 5, 1.0, tab["o_p"]))
    assert not consensus.fit(dry, now=dt.datetime(2025, 8, 1))["heavy"].get("coef")
    out = consensus.predict_heavy({}, None, "day", {"ifs": np.array([1.0])})
    assert out == {5.0: None, 15.0: None}


def test_order_probabilities():
    p5, p15 = consensus.order_probabilities([0.3, np.nan, 0.9], [0.5, 0.4, 0.2], [0.6, 0.1, np.nan])
    np.testing.assert_allclose(p5, [0.3, 0.4, 0.2])
    np.testing.assert_allclose(p15, [0.3, 0.1, np.nan])


def test_frost_scores_count_hits_misses_and_false_alarms():
    rows = pd.DataFrame({"kind": ["night"] * 4 + ["day"], "lead": [1, 1, 1, 1, 1],
                         "o_t": [-1.0, -0.5, 3.0, 4.0, -2.0]})
    f = consensus.frost_scores([-0.2, 1.0, -0.1, 5.0, -3.0], rows)
    assert f["1"] == {"n": 4, "events": 2, "hits": 1, "false_alarms": 1, "bias_on_events": pytest.approx(1.15)}
    assert consensus.frost_scores([5.0, 5.0, 5.0, 5.0, 5.0], rows.assign(o_t=5.0)) is None


def test_finite_makes_scores_valid_json():
    import json
    out = consensus.finite({1: {"pc": float("nan"), "n": np.int64(3), "x": [np.float32(1.5), float("inf")]}})
    assert out == {"1": {"pc": None, "n": 3, "x": [1.5, None]}}
    json.loads(json.dumps(out, allow_nan=False))


def test_issued_log_is_scored_as_issued(tmp_path, monkeypatch):
    """compute logs its station-point forecast; live_scores joins it to the observations."""
    from wxgrid import obs as obs_mod
    from wxgrid.sources import raw

    def fake_fetch(model, init, steps, box, **kw):
        lat = np.arange(box.lat_max, box.lat_min - 0.01, -0.25)
        lon = np.arange(box.lon_min, box.lon_max + 0.01, 0.25)
        return _run(model.key, init, steps, lat, lon, cadence=model.cadence)

    monkeypatch.setattr(raw, "probe", lambda model, *a, **k: model.key != "aifs")     # one member short
    monkeypatch.setattr(raw, "fetch", fake_fetch)
    st = obs_mod.Station("58730", "武夷山", 27.767, 118.033, 221.0)
    issue = dt.datetime(2026, 10, 7, 23, 30)
    prod, _ = native.compute(PTS, county="测试县", seat="甲镇", issue_utc=issue, want_pop=False,
                             calibrate=False, data_dir=tmp_path, stations=[st])
    assert any("ECMWF AIFS" in h for h in prod["meta"]["health"])
    assert any("订正未生效" in h for h in prod["meta"]["health"])
    log = consensus.load_issued(tmp_path / "native")
    assert len(log) == 10 and set(log["lead"]) == {1, 2, 3, 4, 5}
    assert all(list(m) == ["gfs", "ifs"] for m in log["members"])
    # observations for every period: Tmax 30, Tmin 15, dry
    store = obs_mod.ObsStore(tmp_path / "verify" / "obs")
    recs = []
    for d in range(8, 14):
        for h in range(0, 24, 3):
            t = dt.datetime(2026, 10, d, h)
            recs.append({"time": t.strftime("%Y-%m-%dT%H:%M"), "wind_speed": 2.0, "p6": 0.0,
                         **({"tx24": 30.0} if h == 12 else {}), **({"tn24": 15.0} if h == 0 else {})})
    store.merge("58730", recs)
    lv = consensus.live_scores(tmp_path / "native", tmp_path / "verify" / "obs", [st], now=dt.datetime(2026, 10, 14))
    assert lv["issues"] == 1 and lv["short_member_issues"] == 1
    assert lv["scores"][1]["tmax"]["n"] == 1 and lv["scores"][1]["rain"]["n"] == 2
    assert lv["months"]["2026-10"]["issues"] == 1
    # a re-issue of the same run replaces the first one
    consensus.log_issued(tmp_path / "native", [dict(r, t=0.0) for r in log.assign(
        issue=log["issue"].dt.strftime("%Y-%m-%dT%H:%M"), end=log["end"].dt.strftime("%Y-%m-%dT%H:%M")).to_dict("records")])
    again = consensus.load_issued(tmp_path / "native")
    assert len(again) == 10 and (again["t"] == 0.0).all()
    assert obs_mod.latest(tmp_path / "verify" / "obs", [st]) == dt.datetime(2026, 10, 13, 21)


def _fake_members(monkeypatch):
    from wxgrid.sources import raw

    def fake_fetch(model, init, steps, box, **kw):
        lat = np.arange(box.lat_max, box.lat_min - 0.01, -0.25)
        lon = np.arange(box.lon_min, box.lon_max + 0.01, 0.25)
        return _run(model.key, init, steps, lat, lon, cadence=model.cadence)
    monkeypatch.setattr(raw, "probe", lambda *a, **k: True)
    monkeypatch.setattr(raw, "fetch", fake_fetch)


@pytest.mark.parametrize("mean_wind", [0.5, 6.0])     # members' 2.2 m/s is windy / calm against it
def test_night_wind_term_moves_the_3_hourly_curve_with_the_minimum(tmp_path, monkeypatch, mean_wind):
    """The page shows 夜间最低 next to the 3-hourly curve: the curve must not dip below it."""
    _fake_members(monkeypatch)

    def issue(night_wind):
        cal = {"generated": "2026-10-07T23:00:00", "temp": {"day": {}, "night": {}}, "night_wind": night_wind}
        monkeypatch.setattr(consensus, "refresh", lambda *a, **k: cal)
        prod, hdoc = native.compute(PTS, county="测试县", seat="甲镇", issue_utc=dt.datetime(2026, 10, 7, 23, 30),
                                    want_pop=False, data_dir=tmp_path, stations=[], hourly=True)
        return prod, hdoc

    term = {"b": 1.2, "mu": float(np.log(mean_wind)), "n": 500}
    adj = 1.2 * (np.log(np.hypot(2.0, 1.0)) - np.log(mean_wind))
    prod, hdoc = issue(term)
    base, _ = issue(None)
    s, s0 = prod["series3h"], base["series3h"]
    lead = np.array(s["lead_h"])
    nights = 0
    for p, p0 in zip(prod["periods"], base["periods"]):
        sel = (lead - 3 >= p["start_lead"]) & (lead <= p["end_lead"])
        if not sel.any():
            continue
        for c, c0 in zip(p["cells"], p0["cells"]):
            curve = np.array(s["points"][c["point"]]["temp"], dtype=float)[sel]
            curve0 = np.array(s0["points"][c["point"]]["temp"], dtype=float)[sel]
            assert curve.min() >= c["tmin"] - 0.05 and curve.max() <= c["tmax"] + 0.05
            if p["kind"] == "night":
                nights += 1
                assert c["tmin"] - c0["tmin"] == pytest.approx(adj, abs=0.02)
                np.testing.assert_allclose(curve - curve0, adj, atol=0.11)    # series are rounded to 0.1
            else:
                np.testing.assert_allclose(curve, curve0)
    assert nights >= 4
    assert hdoc["meta"]["n_hours"] > 100


@pytest.mark.parametrize("hour_bias", [False, True])
def test_3_hourly_table_reproduces_the_curve_on_the_page(tmp_path, monkeypatch, hour_bias):
    """Rebuilt from the archived runs, the 3-hourly table and ``series_t`` give the page's curve at a
    station, joined to that station's 3-hourly reports: ``temp_3h`` scores what is issued."""
    from wxgrid import obs as obs_mod
    _fake_members(monkeypatch)
    st = obs_mod.Station("58730", "武夷山", 27.767, 118.033, 221.0)
    town = obs_mod.as_townships([st])
    by_lead = lambda a: {str(ld): {"w": {"ifs": 0.5, "aifs": 0.2, "gfs": 0.3}, "a": a, "n": 100}  # noqa: E731
                         for ld in range(1, 6)}
    cal = {"generated": "2026-10-07T23:00:00", "temp": {"day": by_lead(-0.7), "night": by_lead(0.4)},
           "night_wind": {"b": 1.2, "mu": 0.3, "n": 500},
           "temp_3h": {str(ld): {f"{h:02d}": {"a": 0.1 * h - 1.0, "n": 100} for h in consensus.SERIES_HOURS}
                       for ld in range(1, 6)}}
    monkeypatch.setattr(consensus, "refresh", lambda *a, **k: cal)
    monkeypatch.setattr(consensus, "HOUR_BIAS", hour_bias)
    prod, _ = native.compute(town, county="测试县", seat="武夷山", issue_utc=dt.datetime(2026, 10, 7, 23, 30),
                             want_pop=False, data_dir=tmp_path, stations=[])
    obs_mod.ObsStore(tmp_path / "obs").merge("58730", [{"time": f"2026-10-{d:02d}T{h:02d}:00", "t": d + h / 100}
                                                       for d in range(8, 14) for h in range(0, 24, 3)])
    tab = consensus.training_table(tmp_path / "archive", tmp_path / "obs", [st])
    s3 = consensus.series_training_table(tmp_path / "archive", tmp_path / "obs", [st], periods=tab)
    assert len(s3) == 10 * 4
    assert set(s3.loc[s3["kind"] == "day", "hour"]) == {11, 14, 17, 20}
    assert set(s3.loc[s3["kind"] == "night", "hour"]) == {23, 2, 5, 8}
    assert s3["ifs_w"].notna().all(), "every row finds its period (same init, date, kind, lead, end)"
    np.testing.assert_allclose(s3["o_t3"], s3["valid"].dt.day + s3["valid"].dt.hour / 100)

    ext = consensus.period_values(s3, tab, consensus.consensus_t(cal, tab))
    f = consensus.series_t(cal, s3, hour_bias=hour_bias, extreme=ext)
    series = prod["series3h"]
    page = dict(zip(series["times"], series["points"][town[0].id]["temp"]))
    shown = np.array([page.get(t, np.nan) for t in (s3["valid"] + pd.Timedelta(hours=8)).dt.strftime("%Y-%m-%dT%H:00")],
                     dtype=float)
    hit = np.isfinite(shown)
    assert hit.sum() >= 20
    np.testing.assert_allclose(f[hit], shown[hit], atol=0.051)              # the page rounds to 0.1
    if hour_bias:
        assert np.abs(f - consensus.series_t(cal, s3)).max() > 0.5
        day = (s3["kind"] == "day").values
        assert np.isclose(f, ext)[day].any() and np.isclose(f, ext)[~day].any()   # held to 最高 / 最低
        lead = np.array(series["lead_h"])
        for p in prod["periods"]:
            sel = (lead - 3 >= p["start_lead"]) & (lead <= p["end_lead"])
            if not sel.any():
                continue
            curve = np.array(series["points"][town[0].id]["temp"], dtype=float)[sel]
            c = p["cells"][0]
            assert curve.min() >= c["tmin"] - 0.05 and curve.max() <= c["tmax"] + 0.05


def test_uncalibrated_native_refuses_but_still_archives(tmp_path, monkeypatch):
    """Not seeded yet: no member-mean product, but this issue's runs join the training set."""
    _fake_members(monkeypatch)
    monkeypatch.setattr(consensus, "refresh", lambda *a, **k: None)
    with pytest.raises(native.NotCalibrated):
        native.compute(PTS, county="测试县", seat="甲镇", issue_utc=dt.datetime(2026, 10, 7, 23, 30),
                       want_pop=False, data_dir=tmp_path, stations=[], require_calibration=True)
    for src in ("ifs", "aifs", "gfs"):
        assert archive.inits(tmp_path / "archive", src) == [dt.datetime(2026, 10, 7, 12)]
    assert not (tmp_path / "native" / "issued").exists(), "a product that was not issued is not logged"


def _fake_multimodel(calls):
    def fake(points, **kw):
        calls.append("multimodel")
        meta = {"engine": "multimodel", "run": "2026100721", "health": None}
        return {"meta": meta, "periods": []}, None
    return fake


def test_bundle_falls_back_to_multimodel_and_says_so(tmp_path, monkeypatch):
    from wxgrid import product
    calls = []
    _fake_members(monkeypatch)
    monkeypatch.setattr(consensus, "refresh", lambda *a, **k: None)
    monkeypatch.setattr(product, "compute_multimodel", _fake_multimodel(calls))
    prod, _ = product.compute_bundle(PTS, county="测试县", seat="甲镇", engine="native", want_pop=False,
                                     issue_utc=dt.datetime(2026, 10, 7, 23, 30), sess=object(), data_dir=tmp_path)
    assert calls == ["multimodel"]
    fb = prod["meta"]["fallback"]
    assert fb == {"primary": "native", "used": "multimodel",
                  "reasons": [{"engine": "native", "reason": "not_calibrated"}]}
    assert "订正尚未就绪" in prod["meta"]["health"][0] and "Open-Meteo" in prod["meta"]["health"][0]

    # data unreachable on every mirror: same fallback, the other reason
    def broken(*a, **k):
        raise RuntimeError("no gfs mirror served x")
    monkeypatch.setattr(native, "compute", broken)
    prod, _ = product.compute_bundle(PTS, county="测试县", seat="甲镇", engine="native",
                                     issue_utc=dt.datetime(2026, 10, 7, 23, 30), sess=object(), data_dir=tmp_path)
    assert prod["meta"]["fallback"]["reasons"] == [{"engine": "native", "reason": "unavailable"}]
    assert "mirror" not in prod["meta"]["health"][0], "internals stay in the log, not on the page"


def test_bundle_tries_grib_when_native_and_multimodel_both_fail(tmp_path, monkeypatch):
    from wxgrid import product

    class GribReached(Exception):
        pass

    def broken(*a, **k):
        raise RuntimeError("down")

    def choose_run(*a, **k):
        raise GribReached
    monkeypatch.setattr(native, "compute", broken)
    monkeypatch.setattr(product, "compute_multimodel", broken)
    monkeypatch.setattr(product, "choose_run", choose_run)
    with pytest.raises(GribReached):
        product.compute_bundle(PTS, county="测试县", seat="甲镇", engine="native",
                               issue_utc=dt.datetime(2026, 10, 7, 23, 30), sess=object(), data_dir=tmp_path)


def test_mark_fallback_lists_every_engine_tried():
    from wxgrid import product
    prod = {"meta": {"engine": "grib", "health": ["本次未取到 X"]}}
    product._mark_fallback(prod, [("native", "unavailable"), ("multimodel", "unavailable")])
    assert prod["meta"]["fallback"]["primary"] == "native" and prod["meta"]["fallback"]["used"] == "grib"
    first = prod["meta"]["health"][0]
    assert "自主引擎" in first and "Open-Meteo" in first and "未做站点订正" in first
    assert prod["meta"]["health"][1] == "本次未取到 X"
    untouched = {"meta": {"engine": "native"}}
    assert product._mark_fallback(untouched, []) == {"meta": {"engine": "native"}}


def test_health_flags_stale_calibration_and_live_drift():
    now = dt.datetime(2026, 10, 8)
    cal = {"generated": "2026-10-04T00:00:00"}
    sc = {"configs": {"new": {"scores": {"1": {"tmax": {"mae": 1.0}, "tmin": {"mae": 0.9}}}}},
          "live": {"issues": 20, "scores": {"1": {"tmax": {"mae": 1.8}, "tmin": {"mae": 1.0}}}}}
    msgs = consensus.health(cal, sc, members_used=["ifs", "aifs", "gfs"], now=now,
                            obs_latest=dt.datetime(2026, 10, 1))
    assert any("4 天未更新" in m for m in msgs)
    assert any("实况已 7 天" in m for m in msgs)
    assert any("误差大 0.8" in m for m in msgs)
    fresh = {"generated": "2026-10-07T20:00:00"}
    assert consensus.health(fresh, None, members_used=["ifs", "aifs", "gfs"], now=now,
                            obs_latest=dt.datetime(2026, 10, 7, 21)) == []
