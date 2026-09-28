"""Regression tests for the six bugs found at handover.

Each test fails on the pre-fix code and passes after the fix. They use synthetic
grids only — no network — so the suite is deterministic and fast.
"""
from __future__ import annotations

import tempfile

import numpy as np
import pytest
import xarray as xr

from wxgrid import blend, calibrate, daily, downscale, store
from wxgrid.grid import ForecastGrid
from wxgrid.points import Township
from wxgrid.runs import Run
from wxgrid.sources import gfs

INIT = np.datetime64("2026-09-15T18:00")  # 18Z -> valid local hours 05,08,...,02
STEPS = list(range(3, 121, 3))
PTS = [Township("A", "甲", 28.3, 117.7, 60.0), Township("B", "乙", 27.9, 117.6, 600.0)]


def _grid(source, *, precip_rate=None, snow_on_ground=0.0, gust=15.0):
    lat = np.array([28.5, 28.25, 28.0, 27.75])
    lon = np.array([117.25, 117.5, 117.75, 118.0])
    n = len(STEPS)
    shp = (n, lat.size, lon.size)
    rate = np.zeros(n) if precip_rate is None else np.asarray(precip_rate, float)
    tp = np.broadcast_to(np.cumsum(rate)[:, None, None], shp).copy()
    ds = xr.Dataset(
        {
            "t2m": (("step", "latitude", "longitude"), np.full(shp, 25.0)),
            "tmax3": (("step", "latitude", "longitude"), np.full(shp, 27.0)),
            "tmin3": (("step", "latitude", "longitude"), np.full(shp, 23.0)),
            "u10": (("step", "latitude", "longitude"), np.full(shp, 2.0)),
            "v10": (("step", "latitude", "longitude"), np.zeros(shp)),
            "tp": (("step", "latitude", "longitude"), tp),
            "snow": (("step", "latitude", "longitude"), np.full(shp, snow_on_ground)),
            "tcc": (("step", "latitude", "longitude"), np.full(shp, 0.5)),
            "gust": (("step", "latitude", "longitude"), np.full(shp, gust)),
            "orog": (("latitude", "longitude"), np.full((lat.size, lon.size), 100.0)),
        },
        coords={"step": STEPS, "latitude": lat, "longitude": lon,
                "valid_time": ("step", INIT + np.array(STEPS, dtype="timedelta64[h]"))},
        attrs={"init_time": str(INIT)},
    )
    return ForecastGrid(ds, source)


@pytest.fixture
def members():
    e = downscale.apply(_grid("ecmwf-ifs-0p25"), PTS)
    g = downscale.apply(_grid("gfs-0p25"), PTS)
    return e, g


# --- bug 1: blend must keep gust ------------------------------------------
def test_blend_keeps_gust(members):
    e, g = members
    b = blend.combine({"ecmwf-ifs-0p25": e, "gfs-0p25": g})
    assert "gust" in b
    d = daily.to_daily(b)
    # both members report a 15 m/s gust; the daily max must reflect it, not the
    # ~2 m/s mean wind it collapsed to when gust was dropped.
    assert float(d["wind_gust"].max()) == pytest.approx(15.0, abs=1e-6)


# --- bug 2: half_day splits on the window's start hour --------------------
def test_half_day_0508_window_is_night():
    vt_local_h = ((INIT + np.array(STEPS, dtype="timedelta64[h]"))
                  .astype("datetime64[h]").astype(int) + 8) % 24
    rate = np.where(vt_local_h == 8, 5.0, 0.0)  # rain only in the 05->08 window
    ds = downscale.apply(_grid("x", precip_rate=rate), PTS)
    h = daily.half_day(ds)
    assert float(h["day_precip"].sum()) == pytest.approx(0.0)
    assert float(h["night_precip"].sum()) > 0.0


# --- bug 3: precip windows list every local day they touch ----------------
def test_precip_windows_cover_every_wet_day():
    rate = np.zeros(len(STEPS))
    rate[4:28] = 1.0  # steps 15..84h — several local days of steady rain
    ds = downscale.apply(_grid("x", precip_rate=rate), PTS)
    w = daily.precip_windows(ds)
    dly = daily.to_daily(ds)
    wet = [str(d)[:10] for d, p in zip(dly["day"].values, dly["precip"].values[0]) if p >= 0.1]
    got = sorted(k[1] for k in w if k[0] == "A")
    assert got == wet
    # no span may cross local midnight after the split
    assert all(0 <= lo < hi <= 24 for spans in w.values() for lo, hi in spans)


def test_format_windows_no_cross_midnight():
    assert daily.format_windows([(20, 24)]) == "20—24时"
    assert daily.format_windows([(2, 5), (14, 17)]) == "02—05时、14—17时"


# --- bug 4: calibration reaches the daily extremes ------------------------
def test_calibration_shifts_daily_tmax(members):
    e, _ = members
    cal = calibrate.Calibration(bias_t2m={"A": -3.0, "B": -3.0})
    c = calibrate.apply(e, cal)
    d0, d1 = daily.to_daily(e), daily.to_daily(c)
    assert float((d1["tmax"] - d0["tmax"]).mean()) == pytest.approx(-3.0, abs=1e-9)
    assert float((d1["tmin"] - d0["tmin"]).mean()) == pytest.approx(-3.0, abs=1e-9)


def test_calibration_scales_precip_accum(members):
    e, _ = members
    e = e.copy(deep=True)
    e["precip_accum"] = e["precip_accum"] + 5.0  # give it something to scale
    cal = calibrate.Calibration(precip_factor={"A": 2.0, "B": 2.0})
    c = calibrate.apply(e, cal)
    assert float((c["precip_accum"] / e["precip_accum"]).max()) == pytest.approx(2.0, abs=1e-9)


# --- bug 5: NetCDF round trip keeps valid_time ----------------------------
def test_netcdf_round_trip_keeps_valid_time(members):
    e, g = members
    b = blend.combine({"ecmwf-ifs-0p25": e, "gfs-0p25": g})
    with tempfile.TemporaryDirectory() as tmp:
        path = f"{tmp}/fc.nc"
        store.write_netcdf({"ecmwf-ifs-0p25": e, "gfs-0p25": g, "blend": b}, path)
        rt = xr.open_dataset(path)
        assert "valid_time" in rt.coords
        frame = store.to_frame(rt.sel(member="blend"), "blend")
        assert "valid_time" in frame.columns and not frame["valid_time"].isna().any()


# --- bug 6: GFS snowfall is rebased onto the f000 snowpack ----------------
def _raw_gfs_step(step: int, wanted, *, snowpack_mm: float):
    """A cfgrib-style decoded dataset for one lead time (raw shortNames)."""
    lat = np.array([28.5, 28.0])
    lon = np.array([117.5, 118.0])
    shp = (lat.size, lon.size)
    raw = {  # canonical name -> (raw cfgrib name, value)
        "t2m": ("t", 275.0), "u10": ("u", 2.0), "v10": ("v", 0.0),
        "tp": ("tp", 0.0), "snow": ("sdwe", snowpack_mm), "gust": ("gust", 5.0),
        "tcc": ("tcc", 40.0), "orog": ("orog", 100.0),
    }
    data = {}
    targets = set(wanted.values())
    for canon, (rawname, val) in raw.items():
        if canon in targets:
            data[rawname] = (("latitude", "longitude"), np.full(shp, val))
    return xr.Dataset(data, coords={"latitude": lat, "longitude": lon})


def test_gfs_snow_rebased_on_analysis(monkeypatch):
    # Snowpack sits at a constant 40 mm on the ground for the whole forecast:
    # nothing fell, so every per-step snowfall must be zero.
    def fake_read_step(sess, run, step, wanted, attempts=3):
        return (step, wanted)

    def fake_decode(blob):
        step, wanted = blob
        return _raw_gfs_step(step, wanted, snowpack_mm=40.0)

    monkeypatch.setattr(gfs, "_read_step", fake_read_step)
    monkeypatch.setattr(gfs, "decode_grib", fake_decode)

    grid = gfs.fetch(Run.from_stamp("2026091518"), [3, 6, 9], sess=object())
    ds = downscale.apply(grid, PTS)
    assert float(np.nanmax(ds["snow"].values)) == pytest.approx(0.0, abs=1e-6)
