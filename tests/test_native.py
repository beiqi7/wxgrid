"""Native engine data layer: GRIB box decoding, archive, point values, periods. Offline."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest

from wxgrid import archive, gribbox, native
from wxgrid.gribbox import Box, BoxRun
from wxgrid.sources import raw

eccodes = pytest.importorskip("eccodes")


def _grib(values: np.ndarray, *, lon0: float = 0.0, short: str = "2t", south_up: bool = False) -> bytes:
    """A global 1° GRIB2 message (lat 90..-90, lon from ``lon0``) carrying ``values[lat, lon]``."""
    h = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        eccodes.codes_set(h, "Ni", 360)
        eccodes.codes_set(h, "Nj", 181)
        first, last = (-90.0, 90.0) if south_up else (90.0, -90.0)
        eccodes.codes_set(h, "jScansPositively", 1 if south_up else 0)
        eccodes.codes_set(h, "latitudeOfFirstGridPointInDegrees", first)
        eccodes.codes_set(h, "latitudeOfLastGridPointInDegrees", last)
        eccodes.codes_set(h, "longitudeOfFirstGridPointInDegrees", lon0)
        eccodes.codes_set(h, "longitudeOfLastGridPointInDegrees", (lon0 + 359.0) % 360.0)
        eccodes.codes_set(h, "iDirectionIncrementInDegrees", 1.0)
        eccodes.codes_set(h, "jDirectionIncrementInDegrees", 1.0)
        eccodes.codes_set(h, "shortName", short)
        eccodes.codes_set(h, "bitsPerValue", 24)
        eccodes.codes_set_values(h, np.ascontiguousarray(values[::-1] if south_up else values).ravel())
        return eccodes.codes_get_message(h)
    finally:
        eccodes.codes_release(h)


LAT = 90.0 - np.arange(181.0)
LON0 = np.arange(360.0)                       # 0..359 (NCEP order)


def _field_for(lon_order: np.ndarray) -> np.ndarray:
    """A field whose value encodes its own coordinates: 1000*lat + (lon in -180..180)."""
    lon = (lon_order + 180.0) % 360.0 - 180.0
    return LAT[:, None] * 1000.0 + lon[None, :]


BOX = Box(27.0, 30.0, 115.0, 119.0)


def test_split_messages():
    a, b = _grib(_field_for(LON0)), _grib(_field_for(LON0), short="tp")
    parts = gribbox.split_messages(a + b)
    assert parts == [a, b]
    with pytest.raises(ValueError):
        gribbox.split_messages(a[:-10])


@pytest.mark.parametrize("lon0,south_up", [(0.0, False), (180.0, False), (0.0, True)])
def test_decode_box_any_grid_order(lon0, south_up):
    """NCEP (0..360), ECMWF open data (from 180°) and south-up grids cut to the same box."""
    order = (LON0 + lon0) % 360.0
    vals, lat, lon, keys = gribbox.decode_box(_grib(_field_for(order), lon0=lon0, south_up=south_up), BOX)
    assert list(lat) == [30.0, 29.0, 28.0, 27.0]
    assert list(lon) == [115.0, 116.0, 117.0, 118.0, 119.0]
    np.testing.assert_allclose(vals, lat[:, None] * 1000 + lon[None, :], atol=0.05)
    assert keys["units"] == "K"


def test_box_outside_the_grid_is_an_error():
    with pytest.raises(ValueError):
        gribbox.decode_box(_grib(_field_for(LON0)), Box(91.0, 92.0, 0.0, 1.0))


@pytest.mark.parametrize("field,units,x,want", [
    ("t2m", "K", 300.0, 26.85), ("tp", "m", 0.0123, 12.3), ("tp", "kg m**-2", 12.3, 12.3),
    ("tcc", "%", 80.0, 0.8), ("tcc", "(0 - 1)", 0.8, 0.8), ("orog", "m**2 s**-2", 9806.65, 1000.0),
    ("u10", "m s**-1", 3.0, 3.0)])
def test_units_follow_the_message(field, units, x, want):
    """IFS codes tp in m and AIFS in kg m-2; IFS cloud is 0-1 and AIFS/GFS percent."""
    assert gribbox._to_canonical(np.array([x]), units, field)[0] == pytest.approx(want)


# ------------------------------------------------------------------ BoxRun

def _boxrun(source="ifs", steps=range(3, 61, 3), *, init=dt.datetime(2026, 9, 28, 12), t=None, tp_rate=0.0,
            orog=300.0, wind=(3.0, 0.0), extremes=True):
    steps = np.array(list(steps))
    lat = np.array([29.0, 28.5, 28.0, 27.5])
    lon = np.array([117.0, 117.5, 118.0])
    shp = (steps.size, lat.size, lon.size)
    valid_h = np.array([(init + dt.timedelta(hours=int(s))).hour for s in steps])
    # warm afternoons (06 UTC = 14 BJT), cool dawns (21 UTC = 05 BJT)
    diurnal = 20.0 + 6.0 * np.cos((valid_h - 6) / 24.0 * 2 * np.pi) if t is None else np.full(steps.size, t)
    fields = {"t2m": np.broadcast_to(diurnal[:, None, None], shp).copy(),
              "u10": np.full(shp, wind[0]), "v10": np.full(shp, wind[1]),
              "tp": np.broadcast_to((np.arange(steps.size) * tp_rate)[:, None, None], shp).copy()}
    if extremes:
        fields["tmax"] = fields["t2m"] + 0.5
        fields["tmin"] = fields["t2m"] - 0.5
    return BoxRun(source=source, init=init, steps=steps, lat=lat, lon=lon, fields=fields,
                  orog=np.full((lat.size, lon.size), orog), window_start=steps - (steps[1] - steps[0]))


def test_boxrun_roundtrip(tmp_path):
    br = _boxrun()
    p = archive.store(tmp_path, br)
    assert p.name == "2026092812.npz" and p.parent.name == "ifs"
    back = archive.load(tmp_path, "ifs", br.init)
    assert back.source == "ifs" and back.init == br.init
    np.testing.assert_array_equal(back.steps, br.steps)
    for k in br.fields:
        np.testing.assert_allclose(back.fields[k], br.fields[k], rtol=1e-6)
    np.testing.assert_allclose(back.orog, br.orog)
    assert archive.inits(tmp_path, "ifs") == [br.init]
    assert archive.load(tmp_path, "gfs", br.init) is None


def test_prune_drops_old_runs(tmp_path):
    for days in (1, 100):
        br = _boxrun(init=dt.datetime(2026, 10, 1, 12) - dt.timedelta(days=days))
        archive.store(tmp_path, br)
    assert archive.prune(tmp_path, keep_days=75, now=dt.datetime(2026, 10, 1, 12)) == 1
    assert len(archive.inits(tmp_path, "ifs")) == 1


def test_bilinear_reproduces_a_plane():
    br = _boxrun()
    plane = (br.lat[:, None] * 10 + br.lon[None, :])[None]
    got = br.bilinear(np.array([28.25, 27.9]), np.array([117.2, 117.75]), plane)
    np.testing.assert_allclose(got[0], [282.5 + 117.2, 279.0 + 117.75])
    with pytest.raises(ValueError):
        br.bilinear(np.array([31.0]), np.array([117.5]), plane)


def test_point_values_elevation_correction():
    br = _boxrun(orog=300.0, t=20.0)
    pts = [native.Site("low", 28.2, 117.6, 100.0), native.Site("high", 28.2, 117.6, 700.0)]
    std = native.point_values(br, pts, lapse="std")
    assert std["t2m"][0, 0] == pytest.approx(20.0 + 1.3)       # 200 m below the model terrain
    assert std["t2m"][1, 0] == pytest.approx(20.0 - 2.6)       # 400 m above
    assert std["tmax"][0, 0] == pytest.approx(20.5 + 1.3)      # window extremes corrected too
    none = native.point_values(br, pts, lapse="none")
    assert none["t2m"][1, 0] == pytest.approx(20.0)
    assert native.point_values(br, pts, lapse=-0.003)["t2m"][1, 0] == pytest.approx(20.0 - 1.2)
    assert std["u10"][0, 0] == pytest.approx(3.0)               # wind is not corrected


def test_local_lapse_follows_the_model():
    br = _boxrun(t=20.0)
    br.orog = np.array([[0, 200, 400], [0, 200, 400], [0, 200, 400], [0, 200, 400]], dtype=float)
    br.fields["t2m"] = br.fields["t2m"] - 0.004 * br.orog[None]   # model lapse -4 K/km everywhere
    g = br.local_lapse(radius=1, min_relief=100.0)
    np.testing.assert_allclose(g[0], -0.004)
    pts = [native.Site("x", 28.5, 117.5, 600.0)]                   # model terrain 200 m here
    loc = native.point_values(br, pts, lapse="local")
    assert loc["t2m"][0, 0] == pytest.approx(20.0 - 0.8 - 1.6)     # -4 K/km over 400 m


# ------------------------------------------------------------------ periods

def test_period_windows_follow_the_issue_time():
    w12 = native.period_windows(dt.datetime(2026, 9, 28, 12))      # issued 07:30 BJT on the 29th
    assert [(d, k) for d, k, *_ in w12[:3]] == [("2026-09-29", "day"), ("2026-09-30", "night"),
                                                ("2026-09-30", "day")]
    assert len(w12) == 10 and w12[0][2] == dt.datetime(2026, 9, 29, 0)
    w00 = native.period_windows(dt.datetime(2026, 9, 29, 0))       # issued 19:30 BJT -> 今天夜间 first
    assert w00[0][1] == "night" and w00[0][2] == dt.datetime(2026, 9, 29, 12) and len(w00) == 11


def test_period_rows_extremes_precip_wind():
    br = _boxrun(steps=range(3, 145, 3), tp_rate=0.5, wind=(3.0, 4.0))
    rows = native.period_rows(br, [native.Site("s", 28.2, 117.6, 300.0)], lapse="none")
    day1 = next(r for r in rows if r["date"] == "2026-09-29" and r["kind"] == "day")
    night1 = next(r for r in rows if r["date"] == "2026-09-30" and r["kind"] == "night")
    assert day1["lead"] == 1 and night1["lead"] == 1
    assert day1["f_t"] == pytest.approx(26.5)             # 06 UTC peak + 0.5 window margin
    assert night1["f_t"] < 16.0                           # dawn minimum - 0.5
    assert day1["f_p"] == pytest.approx(4 * 0.5)          # four 3 h steps of 0.5 mm
    assert day1["f_w"] == pytest.approx(5.0)
    assert {r["lead"] for r in rows} == {1, 2, 3, 4, 5}


def test_period_rows_without_window_extremes_use_samples():
    """AIFS has no 3 h extremes: three 6-hourly samples per period stand in."""
    br = _boxrun(source="aifs", steps=range(6, 145, 6), extremes=False)
    rows = native.period_rows(br, [native.Site("s", 28.2, 117.6, 300.0)], lapse="none")
    day1 = next(r for r in rows if r["date"] == "2026-09-29" and r["kind"] == "day")
    assert day1["f_t"] == pytest.approx(26.0)             # the 06 UTC sample itself


def test_gfs_window_tiles():
    """GFS max/min windows reset every 6 h (0-3, 0-6, 6-9, 6-12, ...): they still tile a period."""
    assert native._tiles([(12, 15), (12, 18), (18, 21), (18, 24)], 12, 24)
    assert not native._tiles([(12, 15), (18, 21), (18, 24)], 12, 24)
    assert not native._tiles([], 12, 24)


# ------------------------------------------------------------------ catalogues

GFS_IDX = """1:0:d=2025060112:PRMSL:mean sea level:24 hour fcst:
2:100:d=2025060112:TMP:2 m above ground:24 hour fcst:
3:250:d=2025060112:TMAX:2 m above ground:18-24 hour max fcst:
4:400:d=2025060112:APCP:surface:18-24 hour acc fcst:
5:500:d=2025060112:APCP:surface:0-1 day acc fcst:
6:650:d=2025060112:TCDC:entire atmosphere:24 hour fcst:
7:700:d=2025060112:TCDC:entire atmosphere:18-24 hour ave fcst:
8:800:d=2025060112:HGT:surface:24 hour fcst:
9:900:d=2025060112:UGRD:10 m above ground:24 hour fcst:
"""


def test_gfs_catalogue_picks_the_right_messages(monkeypatch):
    monkeypatch.setattr(raw, "get", lambda sess, url, **kw: GFS_IDX.encode())
    jobs = raw._gfs_jobs(raw.GFS, dt.datetime(2025, 6, 1, 12), 24, ("t2m", "tmax", "tp", "tcc"), None)
    got = {j[0]: (j[3], j[4], j[5]) for j in jobs}
    assert got["t2m"] == (100, 150, None)
    assert got["tmax"] == (250, 150, 18)          # window start 18 h
    assert got["tp"] == (500, 150, None)          # the since-init total, not the 6 h bucket
    assert got["tcc"] == (650, 50, None)          # instantaneous, not the window average
    orog = raw._orog_job(raw.GFS, dt.datetime(2025, 6, 1, 12), None, 24)
    assert orog[3:5] == (800, 100)


ECMWF_INDEX = "\n".join([
    '{"param": "2t", "levtype": "sfc", "_offset": 0, "_length": 10}',
    '{"param": "t", "levtype": "pl", "levelist": "850", "_offset": 10, "_length": 10}',
    '{"param": "10fg3", "levtype": "sfc", "_offset": 20, "_length": 10}',
    '{"param": "tp", "levtype": "sfc", "_offset": 30, "_length": 10}',
])


def test_ecmwf_catalogue_with_fallback_names(monkeypatch):
    monkeypatch.setattr(raw, "get", lambda sess, url, **kw: ECMWF_INDEX.encode())
    jobs = raw._ecmwf_jobs(raw.IFS, dt.datetime(2025, 6, 1, 12), 96, ("t2m", "gust", "tp", "tcc"), None)
    got = {j[0]: j[3] for j in jobs}
    assert got == {"t2m": 0, "gust": 20, "tp": 30}      # 10fg3 stands in for 10fg; no tcc that day
    assert jobs[0][2].endswith("/20250601/12z/ifs/0p25/oper/20250601120000-96h-oper-fc.grib2")


def test_model_steps():
    assert raw.IFS.steps(12, 132)[:3] == [3, 6, 9] and raw.IFS.steps(12, 200)[-1] == 144
    assert raw.IFS.steps(6, 132)[-1] == 90
    assert raw.AIFS.steps(0, 30) == [6, 12, 18, 24, 30]
