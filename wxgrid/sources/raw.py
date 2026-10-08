"""Raw global model output, read from the producers' own open-data archives.

No intermediary: each model's GRIB2 files are byte-range read message by message
(from the per-file index) and cut to a small box with :mod:`wxgrid.gribbox`.

=========  ===============================  ==========  =========================================
key        model                            output      licence / host
=========  ===============================  ==========  =========================================
``ifs``    ECMWF IFS HRES 0.25°             3-hourly    ECMWF open data, CC BY 4.0 (AWS mirror)
``aifs``   ECMWF AIFS Single 0.25° (AI)     6-hourly    ECMWF open data, CC BY 4.0 (AWS mirror)
``gfs``    NOAA GFS 0.25°                   1/3-hourly  public domain, NOAA Open Data on AWS
=========  ===============================  ==========  =========================================

All three are free for commercial use (ECMWF asks for attribution). Cycles:
IFS 00/12 UTC reach 240 h (3-hourly to 144 h), 06/18 UTC reach 90 h; AIFS and
GFS run four times a day well past 5 days.

Archive depth on AWS (checked 2026-10): IFS 0.25° since 2024-02, AIFS Single
since 2025-02, GFS since 2021 — enough to backfill a calibration window.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from .. import gribbox
from ..gribbox import Box, BoxRun
from ._fetch import get, head_ok, session

#: ECMWF open data mirrors, tried in order: the AWS copy, then ECMWF's own server.
#: ``WXGRID_ECMWF_BASE`` pins a single one.
ECMWF_BASES = ((os.environ["WXGRID_ECMWF_BASE"],) if os.environ.get("WXGRID_ECMWF_BASE") else
               ("https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com", "https://data.ecmwf.int/forecasts"))
GFS_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"


@dataclasses.dataclass(frozen=True)
class Model:
    key: str
    label: str
    producer: str
    #: canonical field -> candidate message names, first match wins
    params: dict
    #: native output interval (h) up to ``fine_until`` hours
    cadence: int
    #: longest lead (h) per cycle hour
    max_lead: dict
    attribution: str

    def steps(self, cycle_hour: int, last: int, first: int | None = None) -> list[int]:
        """Native output steps from ``first`` (default one interval) to ``last``."""
        top = min(last, self.max_lead[cycle_hour])
        start = self.cadence if first is None else first
        start = int(np.ceil(start / self.cadence) * self.cadence)
        return list(range(start, top + 1, self.cadence))


IFS = Model(
    key="ifs", label="ECMWF IFS 0.25°", producer="ECMWF",
    params={"t2m": ("2t",), "tmax": ("mx2t3",), "tmin": ("mn2t3",), "u10": ("10u",), "v10": ("10v",),
            "tp": ("tp",), "gust": ("10fg", "10fg3"), "tcc": ("tcc",), "snow": ("sf",)},
    cadence=3, max_lead={0: 144, 6: 90, 12: 144, 18: 90},
    attribution="ECMWF open data (CC BY 4.0)")
AIFS = Model(
    key="aifs", label="ECMWF AIFS（AI）", producer="ECMWF",
    params={"t2m": ("2t",), "u10": ("10u",), "v10": ("10v",), "tp": ("tp",), "tcc": ("tcc",), "snow": ("sf",)},
    cadence=6, max_lead={0: 240, 6: 240, 12: 240, 18: 240},
    attribution="ECMWF open data (CC BY 4.0)")
#: GFS selectors: (GRIB name, level, how) — ``since_init`` picks the accumulation from
#: step 0, ``window`` the extreme over the latest window, ``instant`` the state.
GFS = Model(
    key="gfs", label="NOAA GFS 0.25°", producer="NOAA",
    params={"t2m": ("TMP", "2 m above ground", "instant"),
            "tmax": ("TMAX", "2 m above ground", "window"),
            "tmin": ("TMIN", "2 m above ground", "window"),
            "u10": ("UGRD", "10 m above ground", "instant"),
            "v10": ("VGRD", "10 m above ground", "instant"),
            "tp": ("APCP", "surface", "since_init"),
            "gust": ("GUST", "surface", "instant"),
            "tcc": ("TCDC", "entire atmosphere", "instant"),
            "snow": ("WEASD", "surface", "instant")},
    cadence=3, max_lead={0: 240, 6: 240, 12: 240, 18: 240},
    attribution="NOAA GFS (public domain)")

MODELS = {m.key: m for m in (IFS, AIFS, GFS)}
#: Fields the backtest/calibration needs; the live engine asks for everything.
VERIFY_FIELDS = ("t2m", "tmax", "tmin", "u10", "v10", "tp")


# ------------------------------------------------------------------ locations

#: AIFS Single became operational on 2025-02-25; before that the pre-operational
#: AIFS sat under ``aifs/`` with the same layout (2t, 10u, 10v, tp; no cloud).
AIFS_SINGLE_FROM = dt.datetime(2025, 2, 25)


def _ecmwf_dir(model: Model, init: dt.datetime) -> str:
    """Path of a cycle's files relative to a mirror base."""
    if model.key == "aifs":
        product = "aifs-single" if init >= AIFS_SINGLE_FROM else "aifs"
    else:
        product = "ifs"
    return f"{init:%Y%m%d}/{init:%H}z/{product}/0p25/oper"


def _ecmwf_get(sess, rel: str, *, byte_range=None) -> bytes:
    """GET from the first mirror that answers (the AWS copy throttles with 503 SlowDown under load)."""
    last: Exception | None = None
    for base in ECMWF_BASES:
        try:
            return get(sess, f"{base}/{rel}", byte_range=byte_range, timeout=90, retries=6)
        except Exception as exc:  # noqa: BLE001 — try the next mirror
            last = exc
    raise RuntimeError(f"no ECMWF mirror served {rel}") from last


def _ecmwf_stem(init: dt.datetime, step: int) -> str:
    return f"{init:%Y%m%d%H}0000-{step}h-oper-fc"


def _gfs_url(init: dt.datetime, step: int) -> str:
    return f"{GFS_BASE}/gfs.{init:%Y%m%d}/{init:%H}/atmos/gfs.t{init:%H}z.pgrb2.0p25.f{step:03d}"


def probe(model: Model, init: dt.datetime, step: int, sess=None) -> bool:
    """Is the cycle published out to ``step``?"""
    sess = sess or session()
    if model.key == "gfs":
        return head_ok(sess, _gfs_url(init, step) + ".idx")
    rel = f"{_ecmwf_dir(model, init)}/{_ecmwf_stem(init, step)}.index"
    return any(head_ok(sess, f"{base}/{rel}") for base in ECMWF_BASES)


# ------------------------------------------------------------------ message catalogue

_WINDOW = re.compile(r"(\d+)-(\d+) (hour|day) (acc|max|min|ave)")


def _ecmwf_jobs(model: Model, init, step, fields, sess) -> list[tuple]:
    """``[(field, step, url, offset, length, window_start), ...]`` for one lead time."""
    base = f"{_ecmwf_dir(model, init)}/{_ecmwf_stem(init, step)}"
    rows = [json.loads(line) for line in _ecmwf_get(sess, base + ".index").decode("utf-8").splitlines() if line.strip()]
    sfc = {}
    for r in rows:
        if r.get("levtype") == "sfc":
            sfc.setdefault(r["param"], r)
    out = []
    for f in fields:
        for name in model.params.get(f, ()):
            r = sfc.get(name)
            if r is not None:
                out.append((f, step, base + ".grib2", int(r["_offset"]), int(r["_length"]),
                            step - 3 if f in ("tmax", "tmin") else None))
                break
    return out


def _gfs_jobs(model: Model, init, step, fields, sess) -> list[tuple]:
    url = _gfs_url(init, step)
    lines = get(sess, url + ".idx", timeout=60).decode("utf-8").strip().splitlines()
    rows = []
    for line in lines:
        _seq, off, meta = line.split(":", 2)
        bits = meta.split(":")
        m = _WINDOW.search(meta)
        win = None if not m else int(m.group(1)) * (24 if m.group(3) == "day" else 1)
        rows.append((int(off), bits[1], bits[2], win))
    rows.sort()
    out = []
    for f in fields:
        spec = model.params.get(f)
        if spec is None:
            continue
        var, level, how = spec
        cands = [(k, r) for k, r in enumerate(rows) if r[1] == var and r[2] == level]
        if how == "since_init":
            cands = [c for c in cands if c[1][3] == 0]
        elif how == "instant":
            cands = [c for c in cands if c[1][3] is None]
        else:  # window: the latest-starting one
            cands = sorted((c for c in cands if c[1][3] is not None), key=lambda c: -c[1][3])
        if not cands:
            continue
        k, (off, _v, _l, win) = cands[0]
        if k + 1 >= len(rows):
            continue  # last message: length unknown without a HEAD; none of ours is last
        out.append((f, step, url, off, rows[k + 1][0] - off, win if how == "window" else None))
    return out


def _orog_job(model: Model, init, sess, first_step: int) -> tuple | None:
    if model.key == "gfs":
        url = _gfs_url(init, first_step)
        lines = get(sess, url + ".idx", timeout=60).decode("utf-8").strip().splitlines()
        rows = sorted((int(line.split(":")[1]), line) for line in lines)
        for k, (off, line) in enumerate(rows):
            if ":HGT:surface:" in line and k + 1 < len(rows):
                return ("orog", 0, url, off, rows[k + 1][0] - off, None)
        return None
    base = f"{_ecmwf_dir(model, init)}/{_ecmwf_stem(init, 0)}"
    for line in _ecmwf_get(sess, base + ".index").decode("utf-8").splitlines():
        r = json.loads(line)
        if r.get("levtype") == "sfc" and r["param"] == "z":
            return ("orog", 0, base + ".grib2", int(r["_offset"]), int(r["_length"]), None)
    return None


# ------------------------------------------------------------------ fetch

def fetch(model: Model | str, init: dt.datetime, steps, box: Box, *, fields=None, sess=None,
          workers: int = 8, orog: np.ndarray | None = None) -> BoxRun:
    """Read ``fields`` at ``steps`` of one cycle and cut them to ``box``.

    Fields the cycle does not carry (IFS had no cloud in its 2025 open data,
    AIFS has no gust) are simply absent from the result. ``orog`` reuses a
    terrain field already in hand (it is static per model) instead of reading it.
    """
    model = MODELS[model] if isinstance(model, str) else model
    init = init.replace(tzinfo=None, minute=0, second=0, microsecond=0)
    sess = sess or session()
    fields = tuple(fields or model.params)
    steps = sorted({int(s) for s in steps})
    catalogue = _gfs_jobs if model.key == "gfs" else _ecmwf_jobs

    with ThreadPoolExecutor(max_workers=workers) as pool:
        per_step = list(pool.map(lambda s: catalogue(model, init, s, fields, sess), steps))
        jobs = [j for js in per_step for j in js]
        if orog is None:
            oj = _orog_job(model, init, sess, steps[0])
            if oj is not None:
                jobs.append(oj)

        def one(job):
            f, step, url, off, length, win = job
            if model.key == "gfs":
                blob = get(sess, url, byte_range=(off, off + length - 1), timeout=90)
            else:  # ECMWF jobs carry a mirror-relative path
                blob = _ecmwf_get(sess, url, byte_range=(off, off + length - 1))
            vals, lat, lon, keys = gribbox.decode_box(gribbox.split_messages(blob)[0], box)
            return f, step, gribbox._to_canonical(vals, keys.get("units", ""), f), lat, lon, win

        results = list(pool.map(one, jobs))

    if not results:
        raise RuntimeError(f"{model.key} {init:%Y%m%d%H}: nothing to read")
    lat, lon = results[0][3], results[0][4]
    idx = {s: k for k, s in enumerate(steps)}
    data: dict[str, np.ndarray] = {}
    wstart = np.array([s - model.cadence for s in steps], dtype=int)
    terrain = orog
    for f, step, vals, la, lo, win in results:
        if la.shape != lat.shape or lo.shape != lon.shape:
            raise RuntimeError(f"{model.key}: grid changed between messages")
        if f == "orog":
            terrain = vals
            continue
        arr = data.setdefault(f, np.full((len(steps), lat.size, lon.size), np.nan))
        arr[idx[step]] = vals
        if win is not None:
            wstart[idx[step]] = win
    if "snow" in data and model.key == "gfs":
        # WEASD is snow on the ground: re-base on the first step and keep it monotone,
        # so it reads like an accumulation from init (melt is not negative snowfall).
        acc = np.fmax.accumulate(np.nan_to_num(data["snow"] - data["snow"][0]).clip(min=0.0), axis=0)
        data["snow"] = acc
    return BoxRun(source=model.key, init=init, steps=np.array(steps), lat=lat, lon=lon, fields=data,
                  orog=terrain, window_start=wstart,
                  meta={"label": model.label, "attribution": model.attribution, "cadence": model.cadence})


# ------------------------------------------------------------------ ensemble precipitation

GEFS_BASE = "https://noaa-gefs-pds.s3.amazonaws.com"
#: Control + 20 perturbed members on the 0.5° grid.
GEFS_MEMBERS = ("gec00",) + tuple(f"gep{i:02d}" for i in range(1, 21))


def _gefs_url(init: dt.datetime, member: str, step: int) -> str:
    return (f"{GEFS_BASE}/gefs.{init:%Y%m%d}/{init:%H}/atmos/pgrb2ap5/"
            f"{member}.t{init:%H}z.pgrb2a.0p50.f{step:03d}")


def fetch_gefs(init: dt.datetime, steps, box: Box, *, members=GEFS_MEMBERS, sess=None,
               workers: int = 16) -> BoxRun:
    """NOAA GEFS 6-hourly precipitation per member, as accumulation from init.

    Fields are ``tp_<member>``. GEFS ``APCP`` comes in 6 h buckets that tile
    from init, so the 6-hourly ``steps`` are summed back into an accumulation;
    a missing bucket leaves the member NaN from there on.
    """
    init = init.replace(tzinfo=None, minute=0, second=0, microsecond=0)
    sess = sess or session()
    steps = sorted({int(s) for s in steps if int(s) % 6 == 0 and int(s) > 0})
    jobs = [(m, s) for m in members for s in steps]

    def one(job):
        member, step = job
        url = _gefs_url(init, member, step)
        lines = get(sess, url + ".idx", timeout=60).decode("utf-8").strip().splitlines()
        rows = sorted((int(line.split(":")[1]), line) for line in lines)
        for k, (off, line) in enumerate(rows):
            m = _WINDOW.search(line)
            if ":APCP:surface:" in line and m and int(m.group(2)) - int(m.group(1)) == 6 and k + 1 < len(rows):
                blob = get(sess, url, byte_range=(off, rows[k + 1][0] - 1), timeout=90)
                vals, lat, lon, _ = gribbox.decode_box(gribbox.split_messages(blob)[0], box)
                return member, step, vals, lat, lon
        return member, step, None, None, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, jobs))
    got = [r for r in results if r[2] is not None]
    if not got:
        raise RuntimeError(f"gefs {init:%Y%m%d%H}: no APCP read")
    lat, lon = got[0][3], got[0][4]
    idx = {s: k for k, s in enumerate(steps)}
    bucket = {m: np.full((len(steps), lat.size, lon.size), np.nan) for m in members}
    for member, step, vals, _la, _lo in got:
        bucket[member][idx[step]] = vals
    fields = {f"tp_{m}": np.cumsum(b, axis=0) for m, b in bucket.items()}
    return BoxRun(source="gefs", init=init, steps=np.array(steps), lat=lat, lon=lon, fields=fields,
                  meta={"label": "NOAA GEFS 0.5°", "attribution": "NOAA GEFS (public domain)",
                        "members": list(members), "cadence": 6})
