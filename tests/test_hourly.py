"""Hourly series: disaggregation maths, GFS lean fetch, API views, publish flow. Offline."""
from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
import pytest
import xarray as xr

from wxgrid import hourly, phenomena, publish, product
from wxgrid.runs import Run
from wxgrid.sources import gfs
from wxgrid.sources.gefs import EnsemblePrecip
from wxgrid.web import app as webapp

INIT = "2026-09-28T00:00:00"
NODES = list(range(3, 121, 3))
IDS = ["A", "B"]


def _pt_ds(steps, **vars_) -> xr.Dataset:
    """(point, step) dataset; each var is a callable step -> value or a (point, step) array."""
    steps = np.asarray(steps)
    data = {}
    for k, v in vars_.items():
        arr = np.array([[v(s) for s in steps] for _ in IDS], float) if callable(v) else np.asarray(v, float)
        data[k] = (("point", "step"), arr)
    init = np.datetime64(INIT, "s")
    return xr.Dataset(data, coords={
        "point": IDS, "step": steps,
        "valid_time": ("step", init + steps.astype("timedelta64[h]")),
        "name": ("point", ["甲镇", "乙乡"]), "latitude": ("point", [28.3, 28.1]),
        "longitude": ("point", [117.7, 117.6]), "elevation": ("point", [60.0, 600.0]),
    }, attrs={"init_time": INIT, "source": "blend(test)"})


def _blend(precip=lambda s: 0.0):
    return _pt_ds(NODES, t2m=lambda s: 20 + (s % 24) / 3, u10=lambda s: 2.0, v10=lambda s: -1.0,
                  cloud=lambda s: 0.5, gust=lambda s: 6.0, precip=precip, snow=lambda s: 0.0)


def _shape(hours, t2m, precip_accum, cloud=lambda s: 0.5):
    return _pt_ds(hours, t2m=t2m, u10=lambda s: 2.0, v10=lambda s: -1.0, cloud=cloud,
                  gust=lambda s: 6.0, precip_accum=precip_accum)


# ------------------------------------------------------------- disaggregation

def test_shape_hours_fill_the_gaps_and_the_first_window():
    need = hourly.shape_hours(NODES, limit=120)
    assert need[:4] == [1, 2, 4, 5]
    assert not set(need) & set(NODES)
    assert max(need) == 119 and len(need) == 80
    assert max(hourly.shape_hours(range(3, 169, 3), limit=120)) == 119


def test_hourly_equals_the_blend_at_every_node():
    b = _blend()
    shp = _shape(range(0, 121), t2m=lambda s: 15 + np.sin(s), precip_accum=lambda s: 0.0)
    hr = hourly.disaggregate(b, shp)
    assert hr["step"].values[0] == 3 and hr["step"].values[-1] == 120 and hr.sizes["step"] == 118
    at_nodes = hr.sel(step=NODES)
    np.testing.assert_allclose(at_nodes["t2m"].values, b["t2m"].values)
    np.testing.assert_allclose(at_nodes["u10"].values, b["u10"].values)


def test_between_nodes_the_blend_bends_the_way_gfs_bends():
    b = _blend()
    # GFS is flat except for a +2 K spike at hour 4 only
    shp = _shape(range(0, 121), t2m=lambda s: 10.0 + (2.0 if s == 4 else 0.0), precip_accum=lambda s: 0.0)
    hr = hourly.disaggregate(b, shp)
    lin4 = b["t2m"].sel(step=3).values + (b["t2m"].sel(step=6).values - b["t2m"].sel(step=3).values) / 3
    np.testing.assert_allclose(hr["t2m"].sel(step=4).values, lin4 + 2.0)
    lin5 = b["t2m"].sel(step=3).values + 2 * (b["t2m"].sel(step=6).values - b["t2m"].sel(step=3).values) / 3
    np.testing.assert_allclose(hr["t2m"].sel(step=5).values, lin5)


def test_precip_is_split_by_gfs_timing_and_window_totals_are_conserved():
    b = _blend(precip=lambda s: 6.0 if s == 9 else 0.0)  # 6 mm in (6, 9]
    # GFS puts all its rain for that window in hour 8
    shp = _shape(range(0, 121), t2m=lambda s: 20.0, precip_accum=lambda s: 0.0 if s < 8 else 1.0)
    hr = hourly.disaggregate(b, shp)
    p = hr["precip"].sel(step=[7, 8, 9]).values
    np.testing.assert_allclose(p[:, 0], 0.0)
    np.testing.assert_allclose(p[:, 1], 6.0)
    np.testing.assert_allclose(p[:, 2], 0.0)
    # every window that lies fully inside the output adds back up to the blend
    for i, n in enumerate(NODES[1:], start=1):
        win = list(range(NODES[i - 1] + 1, n + 1))
        np.testing.assert_allclose(hr["precip"].sel(step=win).sum("step").values,
                                   b["precip"].sel(step=n).values, atol=1e-9)


def test_dry_gfs_window_shares_blend_rain_equally():
    b = _blend(precip=lambda s: 3.0 if s == 12 else 0.0)
    shp = _shape(range(0, 121), t2m=lambda s: 20.0, precip_accum=lambda s: 0.0)
    hr = hourly.disaggregate(b, shp)
    np.testing.assert_allclose(hr["precip"].sel(step=[10, 11, 12]).values, 1.0)


def test_without_a_shape_source_it_is_plain_linear():
    b = _blend()
    hr = hourly.disaggregate(b, None)
    assert hr.attrs["hourly_shape"] == "linear"
    mid = (b["t2m"].sel(step=3).values * 2 + b["t2m"].sel(step=6).values) / 3
    np.testing.assert_allclose(hr["t2m"].sel(step=4).values, mid)


def test_cloud_stays_a_fraction_and_gust_never_below_mean_wind():
    b = _blend()
    shp = _shape(range(0, 121), t2m=lambda s: 20.0, precip_accum=lambda s: 0.0,
                 cloud=lambda s: 1.0 if s % 3 else 0.0)  # GFS cloud jumps between nodes
    b["cloud"][:] = 0.9
    b["gust"][:] = 0.1
    hr = hourly.disaggregate(b, shp)
    assert float(hr["cloud"].max()) <= 1.0 and float(hr["cloud"].min()) >= 0.0
    assert bool((hr["gust"] >= hr["wind_speed"] - 1e-9).all())


def test_hourly_pop_follows_valid_time_across_cycles():
    # ensemble started 6 h *before* the deterministic run; buckets (0,6], (6,12], ...
    ends = np.arange(6, 133, 6)
    mm = np.zeros((4, 2, ends.size))
    mm[:2, :, 2] = 1.0            # bucket (12, 18] ens-clock = (6, 12] det-clock: 2 of 4 wet
    ens = EnsemblePrecip(mm=mm, starts_h=ends - 6, ends_h=ends, members=("a", "b", "c", "d"),
                         init_time=np.datetime64("2026-09-27T18:00"), point_ids=("A", "B"))
    pop = hourly.hourly_pop(ens, np.datetime64(INIT, "h"), np.arange(3, 121))
    lead = np.arange(3, 121)
    assert np.all(pop[:, (lead >= 7) & (lead <= 12)] == 50.0)
    assert np.all(pop[:, (lead >= 3) & (lead <= 6)] == 0.0)
    assert np.all(pop[:, lead == 13] == 0.0)


def test_build_is_valid_json_in_local_time():
    b = _blend(precip=lambda s: 0.9 if s == 6 else 0.0)
    shp = _shape(range(0, 121), t2m=lambda s: 20.0, precip_accum=lambda s: 0.0)
    hr = hourly.disaggregate(b, shp)
    doc = hourly.build(hr, county="测试县", seat="甲镇", run="2026092800", tz=8.0,
                       pop=np.full((2, hr.sizes["step"]), np.nan))
    text = json.dumps(doc, ensure_ascii=False, allow_nan=False)
    back = json.loads(text)
    assert back["times"][0] == "2026-09-28T11:00"  # 00Z + 3 h, in UTC+8
    assert len(back["times"]) == len(back["lead_h"]) == 118
    a = back["points"]["A"]
    assert a["name"] == "甲镇" and len(a["temp"]) == 118 and a["pop"][0] is None
    assert a["weather"][back["lead_h"].index(5)] == "小雨"  # 0.3 mm/h
    assert back["meta"]["shape"] == "gfs"


@pytest.mark.parametrize("mm,word", [(0.05, "多云"), (0.1, "小雨"), (2.5, "小雨"), (2.6, "中雨"),
                                     (7.6, "中雨"), (7.7, "大雨"), (19.9, "大雨"), (20.0, "强降水")])
def test_hourly_rain_words(mm, word):
    assert phenomena.hour_text(mm, 0.0, 0.6) == word


def test_hourly_snow_words():
    assert phenomena.hour_text(0.5, 0.5, 1.0) == "雪"
    assert phenomena.hour_text(1.0, 0.4, 1.0) == "雨夹雪"


# ------------------------------------------------------------- GFS

def _fake_grib(wanted):
    lat, lon = np.array([28.5, 28.0]), np.array([117.5, 118.0])
    raw = {"t2m": ("t", 290.0), "u10": ("u", 2.0), "v10": ("v", 0.0), "tp": ("tp", 1.0),
           "gust": ("gust", 5.0), "tcc": ("tcc", 50.0), "orog": ("orog", 100.0),
           "snow": ("sdwe", 0.0), "tmax3": ("tmax", 291.0), "tmin3": ("tmin", 289.0)}
    targets = set(wanted.values())
    return xr.Dataset({r: (("latitude", "longitude"), np.full((2, 2), v))
                       for c, (r, v) in raw.items() if c in targets},
                      coords={"latitude": lat, "longitude": lon})


def test_gfs_lean_fetch_reads_orography_once_and_no_extras(monkeypatch):
    calls = []

    def fake_read(sess, run, step, wanted, attempts=3):
        calls.append((step, frozenset(wanted.values())))
        return wanted

    monkeypatch.setattr(gfs, "_read_step", fake_read)
    monkeypatch.setattr(gfs, "decode_grib", _fake_grib)
    grid = gfs.fetch(Run.from_stamp("2026092800"), [4, 5, 7], sess=object(), lean=True, workers=2)
    per_step = [w for s, w in calls if "orog" not in w]
    assert len(per_step) == 3
    assert all("snow" not in w and "tmax3" not in w for w in per_step)
    assert sum("orog" in w for _, w in calls) == 1
    assert grid.steps == [4, 5, 7] and "orog" in grid.ds


def test_gfs_prefers_instantaneous_cloud_and_since_init_rain(monkeypatch):
    idx = "\n".join([
        "1:0:d=2026092800:TCDC:entire atmosphere:3 hour fcst:",
        "2:100:d=2026092800:TCDC:entire atmosphere:0-3 hour ave fcst:",
        "3:200:d=2026092800:APCP:surface:0-3 hour acc fcst:",
        "4:300:d=2026092800:APCP:surface:0-3 hour acc fcst:",
        "5:400:d=2026092800:TMP:2 m above ground:3 hour fcst:",
    ])

    class Sess:
        def head(self, url, timeout=30):
            return type("R", (), {"headers": {"Content-Length": "500"}})()

    monkeypatch.setattr(gfs, "get", lambda sess, url, **kw: idx.encode())
    picked = {}
    monkeypatch.setattr(gfs, "concat_messages", lambda sess, url, spans: picked.setdefault("s", sorted(spans)))
    gfs._step_blob(Sess(), Run.from_stamp("2026092800"), 3,
                   {("TCDC", "entire atmosphere"): "tcc", ("APCP", "surface"): "tp",
                    ("TMP", "2 m above ground"): "t2m"})
    offsets = [o for o, _ in picked["s"]]
    assert 0 in offsets and 100 not in offsets  # instantaneous cloud, not the 0-3 h mean
    assert 200 in offsets and 400 in offsets


@pytest.mark.parametrize("mod", ["gfs", "ecmwf"])
def test_bbox_crop_does_not_pin_the_global_grid(mod):
    """Regression: the crop was a numpy view, so each of ~80 lead times kept its
    full 721x1440 field alive and a publish sat at its 1.1 GB MemoryHigh."""
    from wxgrid.sources import ecmwf
    m = {"gfs": gfs, "ecmwf": ecmwf}[mod]
    lat = np.linspace(90, -90, 721)
    lon = np.linspace(0, 359.75, 1440)
    raw = {"gfs": "t", "ecmwf": "2t"}[mod]
    ds = xr.Dataset({raw: (("latitude", "longitude"), np.full((721, 1440), 290.0))},
                    coords={"latitude": lat, "longitude": lon})
    out = m._normalise(ds, Run.from_stamp("2026092800"), 3, (27.0, 29.5, 116.5, 119.0))
    arr = out["t2m"].values
    assert arr.size < 200
    base = arr.base
    while base is not None and getattr(base, "base", None) is not None:
        base = base.base
    assert base is None or base.nbytes < 10_000, "cropped field still references the global grid"


@pytest.mark.parametrize("raw,short", [("fg10", "10fg"), ("fg10_3", "10fg3")])
def test_ecmwf_gust_survives_cfgrib_naming(raw, short):
    """Regression: cfgrib names 10fg `fg10` (and 10fg3, which open data switches
    to after 90 h, `fg10_3`); the rename missed both, and the blend — which needs
    gust in every member — silently had no gust at all."""
    from wxgrid.sources import ecmwf
    lat, lon = np.array([28.5, 28.25, 28.0]), np.array([117.5, 117.75, 118.0])
    ds = xr.Dataset({raw: (("latitude", "longitude"), np.full((3, 3), 9.0), {"GRIB_shortName": short}),
                     "t2m": (("latitude", "longitude"), np.full((3, 3), 290.0), {"GRIB_shortName": "2t"})},
                    coords={"latitude": lat, "longitude": lon})
    out = ecmwf._normalise(ds, Run.from_stamp("2026092800"), 96)
    assert "gust" in out and raw not in out
    assert float(out["gust"].max()) == pytest.approx(9.0)


def test_ecmwf_index_falls_back_to_the_3h_gust():
    from wxgrid.sources import ecmwf
    rows = [{"levtype": "sfc", "param": "2t", "_offset": 0, "_length": 1},
            {"levtype": "sfc", "param": "10fg3", "_offset": 1, "_length": 1}]
    picked = ecmwf._select(rows, {"2t": "t2m", "10fg": "gust"})
    assert [r["param"] for r in picked] == ["2t", "10fg3"]


# ------------------------------------------------------------- API

def _hourly_doc():
    b = _blend(precip=lambda s: 0.9 if s == 6 else 0.0)
    shp = _shape(range(0, 121), t2m=lambda s: 20.0, precip_accum=lambda s: 0.0)
    return hourly.build(hourly.disaggregate(b, shp), county="测试县", seat="甲镇",
                        run="2026092800", tz=8.0)


@pytest.fixture()
def hserver(tmp_path: pathlib.Path):
    name = "测试县_2026092800.json"
    for d in ("runs", "hourly"):
        (tmp_path / d).mkdir()
    (tmp_path / "runs" / name).write_text('{"meta":{"run":"2026092800"}}', encoding="utf-8")
    (tmp_path / "runs" / "测试县_2026092712.json").write_text('{"meta":{}}', encoding="utf-8")
    (tmp_path / "hourly" / name).write_text(json.dumps(_hourly_doc(), ensure_ascii=False), encoding="utf-8")
    (tmp_path / "latest.json").write_text('{"meta":{"run":"2026092800"}}', encoding="utf-8")
    (tmp_path / "index.json").write_text(json.dumps(
        [{"file": name, "run": "2026092800", "hourly": True},
         {"file": "测试县_2026092712.json", "run": "2026092712", "hourly": False}],
        ensure_ascii=False), encoding="utf-8")
    srv = webapp.build_server("127.0.0.1", 0, str(tmp_path))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def test_api_hourly_all_and_one_township_by_id_or_name(hserver):
    code, doc = _get(f"{hserver}/api/hourly")
    assert code == 200 and set(doc["points"]) == {"A", "B"}

    code, one = _get(f"{hserver}/api/hourly/A")
    assert code == 200 and one["township"]["name"] == "甲镇" and len(one["hours"]) == 118
    h0 = one["hours"][0]
    assert h0["time"] == "2026-09-28T11:00" and h0["lead_h"] == 3 and "temp" in h0 and "weather" in h0

    code, by_name = _get(f"{hserver}/api/hourly/{urllib.parse.quote('乙乡')}?hours=24")
    assert code == 200 and by_name["township"]["id"] == "B" and len(by_name["hours"]) == 24

    code, _ = _get(f"{hserver}/api/health")
    assert code == 200


@pytest.mark.parametrize("path,status", [
    ("/api/hourly/nowhere", 404),
    ("/api/hourly/A?hours=0", 400),
    ("/api/hourly/A?hours=abc", 400),
    ("/api/hourly/A?run=..%2Flatest.json", 400),
    ("/api/hourly/A?run=index.json", 400),
    (f"/api/hourly/A?run={urllib.parse.quote('测试县_2026092712.json')}", 404),  # run without hourly
])
def test_api_hourly_rejects_bad_input(hserver, path, status):
    assert _get(f"{hserver}{path}")[0] == status


# ------------------------------------------------------------- publish flow

@pytest.fixture()
def pub(tmp_path, monkeypatch):
    csv = tmp_path / "t.csv"
    csv.write_text("id,name,lat,lon,elevation_m\nA,甲镇,28.3,117.7,60\n", encoding="utf-8")
    calls = []
    first = {"start": "2026-09-28T20:00"}  # the planned first period; tests move it
    run = Run.from_stamp("2026092800")

    def fake_choose(*a, **k):
        from wxgrid.periods import Period
        return run, [Period("night", first["start"][:10], 12, 24, first["start"], "2026-09-29T08:00")]

    monkeypatch.setattr(product, "choose_run", fake_choose)

    def fake_bundle(pts, **kw):
        calls.append(kw)
        meta = {"county": "测试县", "run": "2026092800", "generated": "2026-09-28T09:00:00+00:00",
                "schema": product.SCHEMA}
        doc = {"meta": meta, "periods": [{"start_local": first["start"]}]}
        return doc, ({"meta": meta, "points": {}} if kw.get("hourly") else None)

    monkeypatch.setattr(product, "compute_bundle", fake_bundle)
    monkeypatch.setattr(publish, "session", lambda: object())
    kw = dict(townships=str(csv), county="测试县", seat="甲镇", data_dir=str(tmp_path / "data"), engine="grib")
    return tmp_path / "data", kw, calls, first


def test_publish_multimodel_skips_the_same_issue_window(tmp_path, monkeypatch):
    csv = tmp_path / "t.csv"
    csv.write_text("id,name,lat,lon,elevation_m\nA,甲镇,28.3,117.7,60\n", encoding="utf-8")
    calls = []
    issue = dt.datetime(2026, 9, 28, 11, 30)

    def fake_bundle(pts, **kw):
        calls.append(kw)
        meta = {"county": "测试县", "run": "2026092809", "generated": "2026-09-28T11:31:00+00:00",
                "schema": product.SCHEMA}
        return {"meta": meta, "periods": [{"start_local": "2026-09-28T20:00"}]}, {"meta": meta, "points": {}}

    monkeypatch.setattr(product, "compute_bundle", fake_bundle)
    monkeypatch.setattr(publish, "session", lambda: object())
    kw = dict(townships=str(csv), county="测试县", seat="甲镇", data_dir=str(tmp_path / "d"), issue_utc=issue)
    out = publish.publish_once(**kw)
    assert out.name == "测试县_2026092809.json" and calls[0]["engine"] == "multimodel"
    assert str(calls[0]["verify_root"]).endswith("/d/verify")
    publish.publish_once(**kw)
    assert len(calls) == 1


def test_publish_native_names_runs_by_issue_window_and_passes_the_data_dir(tmp_path, monkeypatch):
    csv = tmp_path / "t.csv"
    csv.write_text("id,name,lat,lon,elevation_m\nA,甲镇,28.3,117.7,60\n", encoding="utf-8")
    calls = []

    def fake_bundle(pts, **kw):
        calls.append(kw)
        meta = {"county": "测试县", "run": "2026092721", "engine": "native", "generated": "2026-09-27T23:31:00+00:00",
                "schema": product.SCHEMA, "issue_local": "2026-09-28T07:30"}
        return {"meta": meta, "periods": [{"start_local": "2026-09-28T08:00"}]}, {"meta": meta, "points": {}}

    monkeypatch.setattr(product, "compute_bundle", fake_bundle)
    monkeypatch.setattr(publish, "session", lambda: object())
    kw = dict(townships=str(csv), county="测试县", seat="甲镇", data_dir=str(tmp_path / "d"),
              issue_utc=dt.datetime(2026, 9, 27, 23, 30), engine="native")
    out = publish.publish_once(**kw)
    assert out.name == "测试县_2026092721.json"
    assert calls[0]["engine"] == "native" and str(calls[0]["data_dir"]).endswith("/d")
    publish.publish_once(**kw)
    assert len(calls) == 1, "same issue window, same first period: nothing to recompute"


def test_publish_skips_a_cycle_already_on_disk_without_computing(pub):
    data, kw, calls, _ = pub
    publish.publish_once(**kw)
    assert len(calls) == 1
    assert (data / "hourly" / "测试县_2026092800.json").exists()
    assert json.loads((data / "index.json").read_text(encoding="utf-8"))[0]["hourly"] is True
    publish.publish_once(**kw)
    assert len(calls) == 1, "second run on the same cycle must not download anything"


def test_publish_recomputes_when_the_same_cycle_is_issued_for_a_later_period(pub):
    """07:30 and 19:30 can land on the same cycle; the evening product must start at 今天夜间."""
    data, kw, calls, first = pub
    publish.publish_once(**kw)
    first["start"] = "2026-09-29T08:00"
    publish.publish_once(**kw)
    assert len(calls) == 2


def test_publish_recomputes_an_old_format_product(pub):
    data, kw, calls, _ = pub
    (data / "runs").mkdir(parents=True)
    (data / "hourly").mkdir(parents=True)
    (data / "runs" / "测试县_2026092800.json").write_text('{"meta":{"run":"2026092800"},"days":[]}', encoding="utf-8")
    (data / "hourly" / "测试县_2026092800.json").write_text("{}", encoding="utf-8")
    publish.publish_once(**kw)
    assert len(calls) == 1


def test_publish_backfills_hourly_for_an_older_product(pub):
    data, kw, calls, _ = pub
    publish.publish_once(**kw, hourly=False)
    assert len(calls) == 1 and not (data / "hourly" / "测试县_2026092800.json").exists()
    publish.publish_once(**kw)
    assert len(calls) == 2 and (data / "hourly" / "测试县_2026092800.json").exists()


def test_publish_drops_orphan_hourly_files_and_stale_cache(pub, monkeypatch, tmp_path):
    data, kw, _, _ = pub
    cache = tmp_path / "cache"
    cache.mkdir()
    old, fresh = cache / "old.grib2", cache / "fresh.grib2"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    past = time.time() - 48 * 3600
    os.utime(old, (past, past))
    monkeypatch.setenv("WXGRID_CACHE", str(cache))
    (data / "hourly").mkdir(parents=True)
    (data / "hourly" / "测试县_2026090100.json").write_text("{}", encoding="utf-8")
    publish.publish_once(**kw)
    assert not (data / "hourly" / "测试县_2026090100.json").exists()
    assert not old.exists() and fresh.exists()
