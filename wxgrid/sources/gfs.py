"""NOAA GFS 0.25 deg on the AWS Open Data mirror.

Layout::

    https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{YYYYMMDD}/{HH}/atmos/
        gfs.t{HH}z.pgrb2.0p25.f{FFF}
        gfs.t{HH}z.pgrb2.0p25.f{FFF}.idx

The ``.idx`` is plain text — ``seq:offset:d=date:VAR:LEVEL:fcst:`` — so byte
ranges give us ~7 messages instead of the ~540 MB full file.

Caveats this module absorbs:

* GFS is **hourly to 120 h**, then 3-hourly to 384 h.
* ``APCP`` appears twice in the index with identical labels; the first is used.
* ``APCP`` is accumulated from init (``0-N hour acc``), matching ECMWF ``tp``.
* Longitudes are 0..360 and are rolled to -180..180 here.
"""

from __future__ import annotations

import datetime as dt
import re
import time

import numpy as np
import requests
import xarray as xr

from ..grid import ForecastGrid
from ..runs import Run
from ._fetch import concat_messages, decode_grib, get, head_ok, session

BUCKET = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"

#: (GRIB variable, level string) -> canonical name
PARAMS: dict[tuple[str, str], str] = {
    ("TMP", "2 m above ground"): "t2m",
    ("UGRD", "10 m above ground"): "u10",
    ("VGRD", "10 m above ground"): "v10",
    ("APCP", "surface"): "tp",
    ("GUST", "surface"): "gust",
    ("TCDC", "entire atmosphere"): "tcc",
    # WEASD is snow water equivalent *on the ground*, not fresh snowfall. fetch()
    # subtracts the analysis (f000) snowpack so `snow` becomes accumulation from
    # init (like `tp`); the downscaler then differences it into per-step snowfall.
    ("WEASD", "surface"): "snow",
}
#: Extras that improve the daily aggregation; absent fields are simply skipped.
OPTIONAL_PARAMS: dict[tuple[str, str], str] = {
    ("TMAX", "2 m above ground"): "tmax3",
    ("TMIN", "2 m above ground"): "tmin3",
}
STATIC_PARAMS: dict[tuple[str, str], str] = {("HGT", "surface"): "orog"}

#: GRIB variable name -> the name cfgrib gives it (its own ECMWF-style shortName).
CFGRIB_NAME = {"TMP": "t", "UGRD": "u", "VGRD": "v", "APCP": "tp", "GUST": "gust",
               "RH": "r2", "PRES": "sp", "HGT": "orog", "TMAX": "tmax", "TMIN": "tmin",
               "TCDC": "tcc", "WEASD": "sdwe"}

_WINDOW = re.compile(r"(\d+)-(\d+)\s+(hour|day)\s+(?:acc|max|min|ave)")


def _dir_url(run: Run) -> str:
    return f"{BUCKET}/gfs.{run.date:%Y%m%d}/{run.hour:02d}/atmos"


def _stem(run: Run, step: int) -> str:
    return f"gfs.t{run.hour:02d}z.pgrb2.0p25.f{step:03d}"


def _window_start(meta: str) -> int | None:
    """Start hour of the message's accumulation window, e.g. ``18-24 hour acc`` -> 18."""
    m = _WINDOW.search(meta)
    if not m:
        return None
    start, _end, unit = int(m.group(1)), int(m.group(2)), m.group(3)
    return start * (24 if unit == "day" else 1)


def _parse_idx(text: str, total: int) -> list[tuple[str, str, int, int, int | None]]:
    """``[(var, level, offset, length, window_start_h), ...]`` in file order.

    A message ends where the next one starts; the last message ends at EOF.
    """
    rows: list[tuple[int, str, str, int | None]] = []
    for line in text.strip().splitlines():
        _seq, offset, meta = line.split(":", 2)
        bits = meta.split(":")
        rows.append((int(offset), bits[1], bits[2], _window_start(meta)))
    rows.sort()
    return [
        (var, level, offset, (rows[i + 1][0] if i + 1 < len(rows) else total) - offset, win)
        for i, (offset, var, level, win) in enumerate(rows)
    ]


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
    """True when the cycle's index for ``min_step`` exists."""
    return head_ok(sess, f"{_dir_url(run)}/{_stem(run, min_step)}.idx")


def latest_run(sess=None, *, now: dt.datetime | None = None, max_lookback: int = 2,
               min_step: int = 3,
               min_age_hours: float = 1.5) -> Run:
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
            if head_ok(sess, f"{_dir_url(run)}/{_stem(run, min_step)}.idx"):
                return run
    raise RuntimeError("no GFS cycle found in the lookback window")


def available_steps(sess, run: Run) -> list[int]:
    prefix = f"gfs.{run.date:%Y%m%d}/{run.hour:02d}/atmos/gfs.t{run.hour:02d}z.pgrb2.0p25.f"
    steps: set[int] = set()
    token = None
    while True:
        url = f"{BUCKET}/?list-type=2&prefix={prefix}&max-keys=1000"
        if token:
            url += f"&continuation-token={token}"
        body = get(sess, url).decode("utf-8")
        steps |= {int(m) for m in re.findall(r"pgrb2\.0p25\.f(\d{3})(?:</Key>|$)", body)}
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", body)
        if not m:
            break
        token = m.group(1)
    return sorted(steps)


def _step_blob(sess, run: Run, step: int, wanted: dict[tuple[str, str], str]) -> tuple[bytes, int]:
    """Byte-range the wanted messages of one lead time. Returns (blob, bytes_read)."""
    stem = _stem(run, step)
    url = f"{_dir_url(run)}/{stem}"
    total = int(sess.head(url, timeout=30).headers["Content-Length"])
    rows = _parse_idx(get(sess, f"{url}.idx").decode("utf-8"), total)

    # GFS ships several messages of the same variable per file: for APCP both a
    # 6-hour bucket ("18-24 hour acc") and a since-init total ("0-1 day acc"), for
    # TCDC both an instantaneous field and a 6-hour average. Rule: take the
    # since-init accumulation when one exists (so GFS `tp` means the same as ECMWF
    # `tp`), otherwise the first message, which is the instantaneous field.
    best: dict[tuple[str, str], tuple[int, int, int | None]] = {}
    for var, level, offset, length, win in rows:
        key = (var, level)
        if key not in wanted:
            continue
        rank = win if win == 0 else (10**6 if win is None else win + 10**6)
        if key not in best or rank < best[key][2]:
            best[key] = (offset, length, rank)
    spans = [(o, ln) for o, ln, _ in best.values()]
    return concat_messages(sess, url, spans), sum(ln for _, ln, _ in best.values())


def _read_step(sess, run: Run, step: int, wanted: dict[tuple[str, str], str], attempts: int = 3) -> bytes:
    """Read one lead time, refreshing the index if an object changed under us."""
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            blob, _nbytes = _step_blob(sess, run, step, wanted)
            return blob
        except requests.HTTPError as exc:
            last = exc
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"GFS {run} step {step}: byte ranges kept going stale") from last


def _normalise(ds: xr.Dataset, run: Run, step: int, bbox=None) -> xr.Dataset:
    spec = {**PARAMS, **OPTIONAL_PARAMS, **STATIC_PARAMS}
    ren = {CFGRIB_NAME[grib]: canon for (grib, _lev), canon in spec.items() if CFGRIB_NAME[grib] in ds}
    ds = ds.rename(ren)
    for var in ("t2m", "tmax3", "tmin3"):
        if var in ds:
            ds[var] = ds[var] - 273.15
    if "tcc" in ds:
        # GFS codes TCDC in percent; ECMWF tcc is a 0-1 fraction. Blending the
        # two without this turns 晴 into 阴.
        ds["tcc"] = ds["tcc"] / 100.0
    if "t2m" in ds:
        for var in ("tmax3", "tmin3"):
            if var not in ds:
                ds[var] = ds["t2m"]
    ds = ds.assign_coords(longitude=(((ds["longitude"] + 180) % 360) - 180)).sortby("longitude")
    if bbox is not None:
        lat_min, lat_max, lon_min, lon_max = bbox
        ds = ds.sel(latitude=slice(lat_max, lat_min), longitude=slice(lon_min, lon_max))
    ds = ds.expand_dims(step=[step])
    ds = ds.assign_coords(valid_time=("step",
                                           [run.init_time.replace(tzinfo=None)
                                            + np.timedelta64(step, "h")]))
    ds.attrs["init_time"] = run.init_time.replace(tzinfo=None).isoformat()
    ds.attrs["source"] = "gfs-0p25"
    return ds


def fetch(run: Run, steps: list[int], *, params: dict[tuple[str, str], str] | None = None, sess=None,
          bbox: tuple[float, float, float, float] | None = None) -> ForecastGrid:
    """Download and normalise the requested lead times; ``bbox`` crops per lead time."""
    sess = sess or session()
    wanted = {**(params or PARAMS), **OPTIONAL_PARAMS, **STATIC_PARAMS}

    parts = []
    for step in sorted({int(s) for s in steps}):
        blob = _read_step(sess, run, step, wanted)
        ds = _normalise(decode_grib(blob), run, step, bbox)
        for var in ("tp", "snow"):  # f000 carries no accumulation messages
            if var not in ds:
                ds[var] = xr.zeros_like(ds["t2m"])
        parts.append(ds)

    ds = xr.concat(parts, dim="step") if len(parts) > 1 else parts[0]
    if "orog" in ds:  # orography is static; surface pressure is not
        keep = ds["orog"].isel(step=0, drop=True)
        ds = ds.drop_vars("orog").merge(keep)
    if "snow" in ds and 0 not in {int(s) for s in steps}:
        # Rebase onto the analysis (f000) snowpack: WEASD is snow on the ground, so
        # without this the earliest step books pre-existing snow as fresh snowfall.
        base = _normalise(decode_grib(_read_step(sess, run, 0, {("WEASD", "surface"): "snow"})), run, 0, bbox)
        if "snow" in base:
            ds["snow"] = (ds["snow"] - base["snow"].isel(step=0, drop=True)).clip(min=0.0)
    return ForecastGrid(ds.sortby("step"), "gfs-0p25")
