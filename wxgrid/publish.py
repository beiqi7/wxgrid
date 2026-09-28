"""Produce and store forecast products on a schedule.

One run writes an immutable per-cycle JSON under ``runs/`` and repoints
``latest.json`` at it atomically, so a reader never sees a half-written file.
Old runs are pruned to ``keep``. The scheduler is a plain sleep loop (systemd
owns restart/liveness), triggering at fixed local clock times.

Layout under ``--data-dir`` (default ``/var/lib/wxgrid``)::

    runs/<county>_<run>.json    one product per cycle
    latest.json                 -> newest product (the API's default)
    index.json                  [{county, run, init_time, generated}, ...] newest first
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys
import time
from typing import Any

from . import daily as daily_mod
from . import points as points_mod
from . import product
from .sources._fetch import session

DEFAULT_DATA_DIR = os.environ.get("WXGRID_DATA_DIR", "/var/lib/wxgrid")
#: Local clock hours (in --tz) at which the daily job fires.
DEFAULT_HOURS = (7, 18)


def _atomic_write_json(path: pathlib.Path, obj: Any) -> None:
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.part")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def _rebuild_index(data_dir: pathlib.Path) -> list[dict]:
    runs_dir = data_dir / "runs"
    entries = []
    for p in sorted(runs_dir.glob("*.json")):
        try:
            meta = json.loads(p.read_text(encoding="utf-8")).get("meta", {})
        except (ValueError, OSError):
            continue
        entries.append({"file": p.name, "county": meta.get("county"), "run": meta.get("run"),
                        "init_time": meta.get("init_time"), "generated": meta.get("generated"),
                        "member": meta.get("member"), "days": meta.get("days")})
    entries.sort(key=lambda e: (e.get("generated") or "", e.get("run") or ""), reverse=True)
    _atomic_write_json(data_dir / "index.json", entries)
    return entries


def _slug(county: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in county) or "county"


def publish_once(*, townships: str, county: str, seat: str, data_dir: str = DEFAULT_DATA_DIR,
                 days: int = 5, every: int = 3, sources: str = "ecmwf,gfs", member: str = "blend",
                 tz: float = daily_mod.TZ_CHINA, want_pop: bool = True, workers: int = 4,
                 keep: int = 60, min_age_hours: float | None = None,
                 force: bool = False, sess=None) -> pathlib.Path:
    """Compute one product and store it. Returns the run file path.

    Skips recomputation when the newest common cycle is already on disk (saves the
    ~9-minute download), unless ``force``.
    """
    data = pathlib.Path(data_dir)
    (data / "runs").mkdir(parents=True, exist_ok=True)
    pts = points_mod.load_csv(townships)
    sess = sess or session()
    src = tuple(sources.split(","))

    prod = product.compute(pts, county=county, seat=seat, days=days, every=every, sources=src,
                           member=member, tz=tz, want_pop=want_pop, workers=workers,
                           min_age_hours=min_age_hours, sess=sess)
    run_stamp = prod["meta"]["run"]
    out = data / "runs" / f"{_slug(county)}_{run_stamp}.json"
    if out.exists() and not force:
        # Already have this cycle; just make sure latest/index point at the newest.
        _refresh_pointers(data, keep)
        return out
    _atomic_write_json(out, prod)
    _refresh_pointers(data, keep)
    return out


def _refresh_pointers(data: pathlib.Path, keep: int) -> None:
    entries = _rebuild_index(data)
    if entries:
        newest = json.loads((data / "runs" / entries[0]["file"]).read_text(encoding="utf-8"))
        _atomic_write_json(data / "latest.json", newest)
    # prune
    files = sorted((data / "runs").glob("*.json"))
    if len(files) > keep:
        for p in sorted(files, key=lambda p: p.stat().st_mtime)[:-keep]:
            p.unlink(missing_ok=True)
        _rebuild_index(data)


def _seconds_until(hours: tuple[int, ...], tz: float) -> float:
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=tz)
    today = now.replace(minute=0, second=0, microsecond=0)
    cands = [today.replace(hour=h) for h in sorted(hours)]
    cands = [c for c in cands if c > now] or [cands[0] + dt.timedelta(days=1)]
    return (min(cands) - now).total_seconds()


def serve_schedule(*, hours: tuple[int, ...] = DEFAULT_HOURS, tz: float = daily_mod.TZ_CHINA,
                   run_at_start: bool = True, **kw) -> None:
    """Block forever, publishing at each local hour in ``hours``."""
    if run_at_start:
        _safe_publish(**kw)
    while True:
        wait = _seconds_until(hours, tz)
        print(f"[scheduler] next run in {wait/3600:.1f} h", file=sys.stderr, flush=True)
        time.sleep(wait)
        _safe_publish(**kw)


def _safe_publish(**kw) -> None:
    try:
        path = publish_once(**kw)
        print(f"[scheduler] wrote {path}", file=sys.stderr, flush=True)
    except Exception as exc:  # noqa: BLE001 — a scheduler must not die on one bad cycle
        print(f"[scheduler] publish failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wxgrid-publish", description=__doc__)
    p.add_argument("--townships", required=True)
    p.add_argument("--county", default="")
    p.add_argument("--seat", required=True)
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    p.add_argument("--days", type=int, default=5)
    p.add_argument("--every", type=int, default=3)
    p.add_argument("--sources", default="ecmwf,gfs")
    p.add_argument("--member", default="blend")
    p.add_argument("--tz", type=float, default=daily_mod.TZ_CHINA)
    p.add_argument("--no-pop", action="store_true")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--keep", type=int, default=60)
    p.add_argument("--min-age-hours", type=float, default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--loop", action="store_true", help="run forever on the daily schedule")
    p.add_argument("--at", default=",".join(map(str, DEFAULT_HOURS)),
                   help="comma-separated local hours to fire at in --loop mode")
    args = p.parse_args(argv)

    kw = dict(townships=args.townships, county=args.county, seat=args.seat, data_dir=args.data_dir,
              days=args.days, every=args.every, sources=args.sources, member=args.member,
              tz=args.tz, want_pop=not args.no_pop, workers=args.workers, keep=args.keep,
              min_age_hours=args.min_age_hours, force=args.force)
    if args.loop:
        hours = tuple(int(x) for x in args.at.split(","))
        serve_schedule(hours=hours, tz=args.tz, **kw)
        return 0
    path = publish_once(**kw)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
