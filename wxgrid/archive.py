"""Archive of raw model runs cut to the county box — the native engine's training set.

Every live run is stored as it was used (:class:`wxgrid.gribbox.BoxRun`, ~100 kB
per model per cycle), so the station calibration learns from exactly the data
the forecasts are made from. :func:`backfill` fills the same archive from the
producers' historical open-data archives, to start with a full window instead
of waiting weeks for the live runs to accumulate.

Layout under ``root`` (default ``/var/lib/wxgrid/archive``)::

    <model>/<YYYYMMDDHH>.npz
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib
import sys
import time
from typing import Iterable

from .gribbox import Box, BoxRun
from .sources import raw

DEFAULT_ROOT = pathlib.Path(os.environ.get("WXGRID_ARCHIVE_DIR", "/var/lib/wxgrid/archive"))
#: Runs older than this are deleted by :func:`prune` — a 60-day training window
#: plus the 5-day lead and some slack.
KEEP_DAYS = 75


def path(root, source: str, init: dt.datetime) -> pathlib.Path:
    return pathlib.Path(root) / source / f"{init:%Y%m%d%H}.npz"


def inits(root, source: str) -> list[dt.datetime]:
    d = pathlib.Path(root) / source
    if not d.is_dir():
        return []
    out = []
    for p in d.glob("*.npz"):
        try:
            out.append(dt.datetime.strptime(p.stem, "%Y%m%d%H"))
        except ValueError:
            continue
    return sorted(out)


def load(root, source: str, init: dt.datetime) -> BoxRun | None:
    p = path(root, source, init)
    if not p.exists():
        return None
    try:
        return BoxRun.load(p)
    except (OSError, ValueError, KeyError):
        return None


def store(root, run: BoxRun) -> pathlib.Path:
    return run.save(path(root, run.source, run.init))


def prune(root, *, keep_days: int = KEEP_DAYS, now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    cutoff = now - dt.timedelta(days=keep_days)
    gone = 0
    for src in (*raw.MODELS, "gefs"):
        for init in inits(root, src):
            if init < cutoff:
                path(root, src, init).unlink(missing_ok=True)
                gone += 1
    return gone


def backfill(root, *, models: Iterable[str], start: dt.date, end: dt.date, box: Box,
             cycles: Iterable[int] = (0, 12), last_step: int = 144, fields=raw.VERIFY_FIELDS,
             workers: int = 8, sess=None, log=sys.stderr) -> dict[str, int]:
    """Fetch every cycle from ``start`` to ``end`` (inclusive) that is not archived yet.

    A cycle that is missing upstream or fails is logged and skipped; the rest
    continue. Returns the number of runs stored per model.
    """
    done: dict[str, int] = {}
    day = start
    days = []
    while day <= end:
        days.append(day)
        day += dt.timedelta(days=1)
    for key in models:
        model = raw.MODELS.get(key)
        n = 0
        terrain = None
        for d in days:
            for h in cycles:
                init = dt.datetime(d.year, d.month, d.day, h)
                if path(root, key, init).exists():
                    continue
                t0 = time.time()
                try:
                    if key == "gefs":
                        run = raw.fetch_gefs(init, range(6, last_step + 1, 6), box, sess=sess, workers=workers)
                    else:
                        run = raw.fetch(model, init, model.steps(h, last_step), box, fields=fields,
                                        sess=sess, workers=workers, orog=terrain)
                except Exception as exc:  # noqa: BLE001 — one missing cycle must not stop the rest
                    print(f"[backfill] {key} {init:%Y%m%d%H}: {type(exc).__name__}: {exc}", file=log, flush=True)
                    continue
                terrain = run.orog
                store(root, run)
                n += 1
                what = sorted(run.fields) if len(run.fields) <= 9 else f"{len(run.fields)} fields"
                print(f"[backfill] {key} {init:%Y%m%d%H}: {len(run.steps)} steps, {what} "
                      f"in {time.time() - t0:.0f}s", file=log, flush=True)
        done[key] = n
    return done
