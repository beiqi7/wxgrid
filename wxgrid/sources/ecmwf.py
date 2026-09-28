"""ECMWF Open Data — IFS HRES at 0.25 deg.

Layout::

    https://data.ecmwf.int/forecasts/{YYYYMMDD}/{HH}z/ifs/0p25/oper/
        {YYYYMMDD}{HH}0000-{step}h-oper-fc.grib2
        {YYYYMMDD}{HH}0000-{step}h-oper-fc.index      <- JSON-lines, one per message

The ``.index`` carries ``_offset``/``_length`` per message, so a handful of
HTTP range reads yields the whole subset for one lead time instead of the
full ~500 MB file.

Caveats this module absorbs:

* IFS HRES open data is **3-hourly** (steps 0,3,6,...) on every cycle.
* Surface geopotential (``z``) is only present in the 0 h (analysis) file —
  fetch it once per run and reuse it as the model orography.
* ``tp`` is accumulated **from init** in *metres* of water equivalent.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time

import requests

import numpy as np
import xarray as xr

from ..grid import ForecastGrid
from ..runs import Run
from ._fetch import concat_messages, decode_grib, get, head_ok, session

#: Mirrors of the same open-data tree. The AWS mirror comes first because
#: data.ecmwf.int answers ~50 % of a burst with HTTP 429 and no Retry-After,
#: which turns a 40-lead-time fetch into a retry storm. Override with
#: ``WXGRID_ECMWF_BASE``.
BASES = (
    "https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com",
    "https://data.ecmwf.int/forecasts",
)
ROOT = os.environ.get("WXGRID_ECMWF_BASE") or BASES[0]
_active_base: str | None = ROOT if os.environ.get("WXGRID_ECMWF_BASE") else None
_INDEX_CACHE: dict[tuple[str, int], list[dict]] = {}

#: ECMWF shortName -> our canonical name. Kept deliberately lean: every extra
#: field is 721x1440x4 bytes per lead time in memory. Pass ``params=`` for more.
PARAMS = {
    "2t": "t2m",
    "10u": "u10",
    "10v": "v10",
    "tp": "tp",
    "10fg": "gust",
    "tcc": "tcc",
    "sf": "snow",
}
#: Window extremes used for the daily aggregation. ``mx2t3``/``mn2t3`` are the
#: maximum/minimum 2 m temperature over the preceding 3 hours, which beats
#: sampling a 3-hourly instantaneous field for a daily max/min.
OPTIONAL_PARAMS = {
    "mx2t3": "tmax3",
    "mn2t3": "tmin3",
}
STATIC_PARAMS = {"z": "orog", "lsm": "lsm"}


def _path(run: Run) -> str:
    return f"{run.date:%Y%m%d}/{run.hour:02d}z/ifs/0p25/oper"


def _mirror_order() -> tuple[str, ...]:
    active = _active_base or ROOT
    return (active, BASES[0] if active == BASES[1] else BASES[1])


def _mirrored_get(sess, path: str, *, byte_range: tuple[int, int] | None = None,
                  attempts: int = 8, timeout: int = 60) -> bytes:
    """Fetch ``path`` alternating mirrors.

    Neither mirror is reliable on its own: in a burst the AWS copy answers ~40 %
    of requests with 503 SlowDown and data.ecmwf.int answers ~40 % with 429.
    Alternating, with a short pause between rounds, gets through both.
    """
    hit = _mirror_order()
    last: Exception | None = None
    for attempt in range(attempts):
        base = hit[attempt % 2]
        try:
            blob = get(sess, f"{base}/{path}", byte_range=byte_range, timeout=timeout, retries=1)
            _remember_base(base)
            return blob
        except (requests.HTTPError, requests.RequestException, RuntimeError) as exc:
            last = exc
            time.sleep(0.4 * (attempt + 1))
    raise RuntimeError(f"both ECMWF mirrors refused {path}") from last


def _remember_base(base: str) -> None:
    global _active_base
    _active_base = base


def _dir_url(run: Run, base: str | None = None) -> str:
    return f"{base or _active_base or ROOT}/{_path(run)}"


def _pick_base(sess, run: Run) -> str:
    """Sticky working mirror, or the configured default before anything has run."""
    return _active_base or ROOT


def _stem(run: Run, step: int) -> str:
    return f"{run.stamp}0000-{step}h-oper-fc"


def candidate_runs(*, now: dt.datetime | None = None, max_lookback: int = 2,
                   min_age_hours: float = 2.0) -> list[Run]:
    """Cycles worth considering, newest first (existence is checked separately)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out: list[Run] = []
    for back in range(max_lookback + 1):
        day = (now - dt.timedelta(days=back)).date()
        for hour in (18, 12, 6, 0):
            run = Run(day, hour)
            if (now - run.init_time) >= dt.timedelta(hours=min_age_hours):
                out.append(run)
    return out


def probe_run(sess, run: Run, min_step: int) -> bool:
    """True when the cycle's index for ``min_step`` exists on any mirror."""
    rel = f"{_path(run)}/{_stem(run, min_step)}.index"
    return any(head_ok(sess, f"{base}/{rel}") for base in _mirror_order())


def latest_run(sess=None, *, now: dt.datetime | None = None, max_lookback: int = 2,
               min_step: int = 0,
               min_age_hours: float = 2.0) -> Run:
    """Most recent cycle that is both deep enough and settled.

    ``min_step`` skips cycles whose later lead times have not been posted yet and
    ``min_age_hours`` skips cycles that are still being written — reading a file
    mid-publication yields stale byte offsets.
    """
    sess = sess or session()
    now = now or dt.datetime.now(dt.timezone.utc)
    for back in range(max_lookback + 1):
        day = (now - dt.timedelta(days=back)).date()
        for hour in (18, 12, 6, 0):
            run = Run(day, hour)
            if (now - run.init_time) < dt.timedelta(hours=min_age_hours):
                continue
            probe = f"{_dir_url(run)}/{_stem(run, 0)}.index" if min_step == 0 else \
                    f"{_dir_url(run)}/{_stem(run, min_step)}.index"
            if head_ok(sess, probe):
                return run
    raise RuntimeError("no ECMWF open-data cycle found in the lookback window")


def available_steps(sess, run: Run) -> list[int]:
    """Lead times the cycle publishes, from whichever mirror is active."""
    path = _path(run)
    body = _mirrored_get(sess, f"?list-type=2&prefix={path}/&max-keys=1000").decode("utf-8", "replace")
    if "-" not in body:  # non-S3 mirror: HTML directory listing
        body = _mirrored_get(sess, f"{path}/").decode("utf-8", "replace")
    return sorted({int(m) for m in re.findall(r"-(\d+)h-oper-fc\.grib2", body)})


def _index(sess, run: Run, step: int, *, refresh: bool = False) -> list[dict]:
    """Message index for one lead time, memoised — it is re-read on every retry."""
    key = (run.stamp, step)
    if not refresh and key in _INDEX_CACHE:
        return _INDEX_CACHE[key]
    rel = f"{_path(run)}/{_stem(run, step)}.index"
    raw = _mirrored_get(sess, rel).decode("utf-8")
    rows = [json.loads(line) for line in raw.strip().splitlines()]
    _INDEX_CACHE[key] = rows
    return rows


def _select(rows: list[dict], params: dict[str, str], *, required: bool = True) -> list[dict]:
    out = []
    for name in params:
        hit = next((r for r in rows if r.get("levtype") == "sfc" and r["param"] == name), None)
        if hit is None:
            if required:
                raise KeyError(f"ECMWF index has no surface message for {name!r}")
            continue
        out.append(hit)
    return out


def _normalise(ds: xr.Dataset, run: Run, step: int, bbox=None) -> xr.Dataset:
    ren = {k: v for k, v in {**PARAMS, **OPTIONAL_PARAMS, **STATIC_PARAMS}.items() if k in ds}
    ds = ds.rename(ren)
    for var in ("t2m", "d2m", "tmax3", "tmin3"):
        if var in ds:
            ds[var] = ds[var] - 273.15
    if "t2m" in ds:  # degenerate window (analysis) — fall back to the instantaneous field
        for var in ("tmax3", "tmin3"):
            if var not in ds:
                ds[var] = ds["t2m"]
    for var in ("tp", "snow"):
        if var in ds:
            ds[var] = ds[var] * 1000.0  # m water equivalent -> mm
    if "orog" in ds:
        ds["orog"] = ds["orog"] / 9.80665  # geopotential m2/s2 -> m
    ds = ds.assign_coords(longitude=(((ds["longitude"] + 180) % 360) - 180)).sortby("longitude")
    if bbox is not None:
        lat_min, lat_max, lon_min, lon_max = bbox
        ds = ds.sel(latitude=slice(lat_max, lat_min), longitude=slice(lon_min, lon_max))
    ds = ds.expand_dims(step=[step])
    ds = ds.assign_coords(valid_time=("step",
                                           [run.init_time.replace(tzinfo=None)
                                            + np.timedelta64(step, "h")]))
    ds.attrs["init_time"] = run.init_time.replace(tzinfo=None).isoformat()
    ds.attrs["source"] = "ecmwf-ifs-0p25"
    return ds


def _read_step(sess, run: Run, step: int, params: dict[str, str], attempts: int = 4) -> bytes:
    """Read one lead time, refreshing the index on a short read.

    ECMWF rewrites/repacks a cycle's files while it is still being published, so
    offsets taken from an index fetched a minute ago can be stale. Re-read the
    index and try again rather than returning a corrupt GRIB blob.
    """
    rel = f"{_path(run)}/{_stem(run, step)}.grib2"
    last: Exception | None = None
    for attempt in range(attempts):
        rows = _index(sess, run, step, refresh=attempt > 0)
        sel = _select(rows, params, required=False)
        try:
            return b"".join(_mirrored_get(sess, rel, byte_range=(r["_offset"], r["_offset"] + r["_length"] - 1),
                                          attempts=3)
                            for r in sel)
        except RuntimeError as exc:
            last = exc
            time.sleep(1.0 * (attempt + 1))
    raise RuntimeError(f"ECMWF {run} step {step}: no mirror would serve the byte ranges") from last


def fetch(run: Run, steps: list[int], *, params: dict[str, str] | None = None, sess=None,
          bbox: tuple[float, float, float, float] | None = None) -> ForecastGrid:
    """Download and normalise the requested lead times.

    ``bbox`` crops each lead time *as it arrives*; without it the whole globe is
    held for every step, which is what blows up a 120 h request.
    """
    sess = sess or session()
    static = {k: v for k, v in STATIC_PARAMS.items()}

    params = {**(params or PARAMS), **OPTIONAL_PARAMS}
    parts = []
    for step in sorted({int(s) for s in steps}):
        blob = _read_step(sess, run, step, params)
        parts.append(_normalise(decode_grib(blob), run, step, bbox))

    ds = xr.concat(parts, dim="step") if len(parts) > 1 else parts[0]

    # static fields ride along once per run, taken from the analysis file
    ds0 = _normalise(decode_grib(_read_step(sess, run, 0, static)), run, 0, bbox)
    ds0 = ds0.isel(step=0, drop=True)  # static fields carry no lead time
    ds = xr.merge([ds, ds0], compat="override", join="exact")
    return ForecastGrid(ds.sortby("step"), "ecmwf-ifs-0p25")
