"""NOAA GEFS 0.5 deg — the precipitation-probability source.

A deterministic run cannot produce a probability, so 降水概率 comes from the
GEFS ensemble (20 perturbed members + control on the 0.5 deg grid, plus the
30-member extended set). Only ``APCP`` is fetched, which is ~0.09 MB per member
per lead time — 31 members x 20 lead times is ~56 MB, against ~900 MB for the
ECMWF 0.25 deg ensemble over the same span.

GEFS ``APCP`` arrives as 6-hour buckets that tile from init (``0-6 hour acc``,
``6-12 hour acc``, ...), so a bucket has to be split across a local-day boundary
in proportion to its overlap. That is done in :mod:`wxgrid.probability`.
"""

from __future__ import annotations

import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np
import xarray as xr

from ..runs import Run
from ._fetch import decode_grib, get, head_ok, session
from .gfs import _parse_idx, _window_start

BUCKET = "https://noaa-gefs-pds.s3.amazonaws.com"

#: Control + 20 perturbed members on the 0.50 deg grid. gep21-30 need pgrb2bp5.
MEMBERS = ("gec00",) + tuple(f"gep{i:02d}" for i in range(1, 21))

WANTED = ("APCP", "surface")


def _dir_url(run: Run) -> str:
    return f"{BUCKET}/gefs.{run.date:%Y%m%d}/{run.hour:02d}/atmos/pgrb2ap5"


def _stem(run: Run, member: str, step: int) -> str:
    return f"{member}.t{run.hour:02d}z.pgrb2a.0p50.f{step:03d}"


def probe_run(sess, run: Run, min_step: int) -> bool:
    return head_ok(sess, f"{_dir_url(run)}/{_stem(run, MEMBERS[0], min_step)}.idx")


def candidate_runs(*, now: dt.datetime | None = None, max_lookback: int = 2,
                   min_age_hours: float = 2.0) -> list[Run]:
    now = now or dt.datetime.now(dt.timezone.utc)
    out: list[Run] = []
    for back in range(max_lookback + 1):
        day = (now - dt.timedelta(days=back)).date()
        for hour in (18, 12, 6, 0):
            run = Run(day, hour)
            if (now - run.init_time) >= dt.timedelta(hours=min_age_hours):
                out.append(run)
    return out


def available_steps(sess, run: Run, member: str = MEMBERS[0]) -> list[int]:
    import re

    prefix = f"gefs.{run.date:%Y%m%d}/{run.hour:02d}/atmos/pgrb2ap5/{member}.t{run.hour:02d}z.pgrb2a.0p50.f"
    body = get(sess, f"{BUCKET}/?list-type=2&prefix={prefix}&max-keys=1000").decode()
    return sorted({int(m) for m in re.findall(r"0p50\.f(\d{3})", body)})


@dataclass(frozen=True)
class EnsemblePrecip:
    """Per-member 6-hour precipitation buckets at a set of points.

    ``mm`` has shape ``(member, point, bucket)``; ``starts_h``/``ends_h`` are the
    bucket bounds in hours after model init.
    """

    mm: np.ndarray
    starts_h: np.ndarray
    ends_h: np.ndarray
    members: tuple[str, ...]
    init_time: np.datetime64
    point_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.mm.shape[0] != len(self.members):
            raise ValueError("member axis mismatch")
        if np.any(self.mm < -1e-6):
            raise ValueError("negative precipitation in ensemble")




def fetch_ensemble(points, run: Run, steps: list[int], *, members=MEMBERS, sess=None,
                   max_workers: int = 8) -> EnsemblePrecip:
    """Download APCP for every member/lead time and interpolate to ``points``."""
    sess = sess or session()
    lat = np.array([p.lat for p in points], dtype=float)
    lon = np.array([p.lon for p in points], dtype=float)
    steps = sorted({int(s) for s in steps})

    jobs = [(m, s) for m in members for s in steps]

    def one(job):
        member, step = job
        url = f"{_dir_url(run)}/{_stem(run, member, step)}"
        # APCP sits in the middle of the file, so its length comes from the next
        # index entry and the object size (an extra HEAD per member) is not needed.
        rows = _parse_idx(get(sess, f"{url}.idx").decode("utf-8"), 0)

        cands = [r for r in rows if (r[0], r[1]) == WANTED]
        if not cands:
            raise KeyError(f"{member} f{step:03d}: no APCP message")
        cands.sort(key=lambda r: (r[4] if r[4] is not None else 10**6))
        var, level, offset, length, win = cands[0]
        if length <= 0:
            raise RuntimeError(f"{member} f{step:03d}: APCP is the last message; size unknown")

        blob = get(sess, url, byte_range=(offset, offset + length - 1))
        ds = decode_grib(blob)
        name = "tp" if "tp" in ds else next(iter(ds.data_vars))
        da = ds[name].squeeze(drop=True).load()
        da = da.assign_coords(longitude=(((da["longitude"] + 180) % 360) - 180)).sortby("longitude")
        da = da.sel(latitude=slice(lat.max() + 0.5, lat.min() - 0.5),
                    longitude=slice(lon.min() - 0.5, lon.max() + 0.5))
        sub = da.interp(latitude=xr.DataArray(lat, dims="point"),
                        longitude=xr.DataArray(lon, dims="point"), method="linear")
        start = win if win is not None else max(step - 6, 0)
        return member, step, start, np.asarray(sub.values, dtype=float)

    mm = np.full((len(members), len(points), len(steps)), np.nan)
    starts = np.zeros(len(steps), dtype=int)
    ends = np.array(steps, dtype=int)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for member, step, start, vals in pool.map(one, jobs):
            i = steps.index(step)
            mm[members.index(member), :, i] = vals
            starts[i] = start

    if np.isnan(mm).any():
        raise RuntimeError("ensemble fetch left gaps")
    return EnsemblePrecip(mm=mm, starts_h=starts, ends_h=ends, members=tuple(members),
                          init_time=np.datetime64(run.init_time.replace(tzinfo=None)),
                          point_ids=tuple(str(p.id) for p in points))
