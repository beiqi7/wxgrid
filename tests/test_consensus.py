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
