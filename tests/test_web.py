"""Regressions for the read-only API and the JSON product builder."""
from __future__ import annotations

import json
import pathlib
import threading
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
import pytest
import xarray as xr

from wxgrid import blend, daily, downscale, product
from wxgrid.grid import ForecastGrid
from wxgrid.points import Township
from wxgrid.web import app as webapp

INIT = np.datetime64("2026-09-15T18:00")
STEPS = list(range(3, 121, 3))
PTS = [Township("A", "甲镇", 28.30, 117.70, 60.0),
       Township("B", "乙乡", 28.10, 117.60, 600.0)]


def _grid(source: str, *, precip_rate=None) -> ForecastGrid:
    lat = np.array([28.75, 28.50, 28.25, 28.00, 27.75])
    lon = np.array([117.25, 117.50, 117.75, 118.00])
    n = len(STEPS)
    shp = (n, lat.size, lon.size)
    rate = np.zeros(n) if precip_rate is None else np.asarray(precip_rate, float)
    ds = xr.Dataset(
        {
            "t2m": (("step", "latitude", "longitude"), np.full(shp, 25.0)),
            "tmax3": (("step", "latitude", "longitude"), np.full(shp, 29.0)),
            "tmin3": (("step", "latitude", "longitude"), np.full(shp, 21.0)),
            "u10": (("step", "latitude", "longitude"), np.full(shp, 3.0)),
            "v10": (("step", "latitude", "longitude"), np.zeros(shp)),
            "gust": (("step", "latitude", "longitude"), np.full(shp, 14.0)),
            "tp": (("step", "latitude", "longitude"),
                   np.broadcast_to(np.cumsum(rate)[:, None, None], shp).copy()),
            "snow": (("step", "latitude", "longitude"), np.zeros(shp)),
            "tcc": (("step", "latitude", "longitude"), np.full(shp, 0.6)),
            "orog": (("latitude", "longitude"), np.full((lat.size, lon.size), 150.0)),
        },
        coords={"step": STEPS, "latitude": lat, "longitude": lon,
                "valid_time": ("step", INIT + np.array(STEPS, dtype="timedelta64[h]"))},
        attrs={"init_time": str(INIT)},
    )
    return ForecastGrid(ds, source)


def _product(precip_rate=None) -> dict:
    e = downscale.apply(_grid("ecmwf-ifs-0p25", precip_rate=precip_rate), PTS)
    g = downscale.apply(_grid("gfs-0p25", precip_rate=precip_rate), PTS)
    b = blend.combine({"ecmwf-ifs-0p25": e, "gfs-0p25": g})
    dly = daily.to_daily(b)
    halves = daily.half_day(b)
    windows = daily.precip_windows(b)
    return product.build(dly, halves, windows, county="测试县", seat="甲镇",
                         run="2026091518", member="blend",
                         sources=("ecmwf", "gfs"), weights=None,
                         tz=daily.TZ_CHINA, days=5)


# ---------------------------------------------------------------- product

def test_product_is_json_serialisable_with_no_nan():
    """NaN is not valid JSON; every numeric field must be a number or null."""
    doc = _product()
    text = json.dumps(doc, ensure_ascii=False, allow_nan=False)
    assert "NaN" not in text
    assert json.loads(text)["meta"]["county"] == "测试县"


def test_product_shape_matches_townships_and_days():
    doc = _product()
    assert len(doc["days"]) == 5
    assert len(doc["townships"]) == len(PTS)
    for day in doc["days"]:
        assert len(day["cells"]) == len(PTS)
        assert {c["point"] for c in day["cells"]} == {"A", "B"}


def test_product_keeps_gust_so_bulletin_can_report_it():
    """Regression: the blend used to drop `gust`, so 阵风 never appeared."""
    doc = _product()
    gusts = [c["wind_gust"] for d in doc["days"] for c in d["cells"]]
    assert max(gusts) == pytest.approx(14.0)
    assert "阵风" in doc["text"]


def test_conclusions_name_the_seat_township():
    """Regression: seat_id was computed but dropped, so the web hero fell back to
    the first township in the roster instead of the county seat."""
    doc = _product()
    assert doc["conclusions"]["seat_id"] == "A"  # seat="甲镇" -> id "A"


def test_rain_alert_fires_on_a_heavy_day():
    rate = np.zeros(len(STEPS))
    rate[8:16] = 9.0  # ~72 mm inside one local day
    doc = _product(precip_rate=rate)
    kinds = {a["type"] for a in doc["conclusions"]["alerts"]}
    assert "暴雨" in kinds


def test_multi_day_rain_lists_a_window_on_every_wet_day():
    """Regression: a span crossing local midnight was keyed only to its start day."""
    rate = np.zeros(len(STEPS))
    rate[4:28] = 1.0
    doc = _product(precip_rate=rate)
    wet = [d for d in doc["days"] if (d["cells"][0]["precip"] or 0) >= 0.1]
    assert len(wet) >= 3
    for d in wet:
        assert d["cells"][0]["windows"], f"{d['date']} is wet but lists no window"


# ---------------------------------------------------------------- API

@pytest.fixture()
def server(tmp_path: pathlib.Path):
    doc = _product()
    runs = tmp_path / "runs"
    runs.mkdir()
    name = "测试县_2026091518.json"
    (runs / name).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "latest.json").write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "index.json").write_text(json.dumps(
        [{"file": name, "county": "测试县", "run": "2026091518"}], ensure_ascii=False), encoding="utf-8")

    srv = webapp.build_server("127.0.0.1", 0, str(tmp_path))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", name
    srv.shutdown()
    srv.server_close()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read(), dict(r.headers)


def _status(url: str) -> int:
    try:
        return _get(url)[0]
    except urllib.error.HTTPError as exc:
        return exc.code


def test_api_serves_latest_and_summary(server):
    base, _ = server
    code, body, _ = _get(f"{base}/api/latest")
    assert code == 200
    assert json.loads(body)["meta"]["county"] == "测试县"

    code, body, _ = _get(f"{base}/api/summary")
    assert code == 200
    small = json.loads(body)
    assert "conclusions" in small and "cells" not in json.dumps(small)


def test_api_fetches_a_run_with_a_chinese_filename(server):
    """Regression: the handler did not percent-decode, so CJK run ids 404'd."""
    base, name = server
    code, body, _ = _get(f"{base}/api/runs/{urllib.parse.quote(name)}")
    assert code == 200
    assert json.loads(body)["meta"]["run"] == "2026091518"


@pytest.mark.parametrize("bad", [
    "../latest.json",
    "..%2Flatest.json",
    "%2e%2e%2flatest.json",
    "latest.json",
    "index.json",
])
def test_api_rejects_traversal_and_reserved_names(server, bad):
    base, _ = server
    assert _status(f"{base}/api/runs/{bad}") in (400, 404)


def test_static_assets_revalidate_instead_of_going_stale(server):
    """A redeploy must not keep serving a cached bundle."""
    base, _ = server
    code, body, headers = _get(f"{base}/static/app.js")
    assert code == 200 and body
    assert headers.get("Cache-Control") == "no-cache"
    etag = headers["ETag"]

    req = urllib.request.Request(f"{base}/static/app.js", headers={"If-None-Match": etag})
    try:  # urllib raises on 304, which is exactly the success case here
        with urllib.request.urlopen(req, timeout=10) as r:
            assert r.status == 304
    except urllib.error.HTTPError as exc:
        assert exc.code == 304


def test_unknown_route_is_404(server):
    base, _ = server
    assert _status(f"{base}/api/nope") == 404


def test_every_id_the_ui_script_uses_exists_in_the_page():
    """Regression: app.js and index.html drifted apart during the UI rewrite and
    the page rendered blank because getElementById returned null."""
    import re
    static = pathlib.Path(webapp.__file__).parent / "static"
    js = (static / "app.js").read_text(encoding="utf-8")
    html = (static / "index.html").read_text(encoding="utf-8")
    used = set(re.findall(r"\$\('([\w-]+)'\)", js))
    have = set(re.findall(r'id="([\w-]+)"', html))
    # ids the script creates itself (inside innerHTML templates) are fine too
    have |= set(re.findall(r'id="([\w-]+)"', js))
    assert used, "no $('id') lookups found; the regex no longer matches app.js"
    assert used <= have, f"app.js looks up ids missing from index.html: {sorted(used - have)}"
