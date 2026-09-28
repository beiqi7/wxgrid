"""End-to-end: run discovery -> byte-range download -> downscale -> blend -> persist."""

from __future__ import annotations

import numpy as np
import xarray as xr

from . import blend as blending
from . import downscale
from .points import Township
from .sources import REGISTRY
from .sources._fetch import session


def bbox_of(points: list[Township], pad: float = 0.75) -> tuple[float, float, float, float]:
    lat = np.array([p.lat for p in points])
    lon = np.array([p.lon for p in points])
    return (float(lat.min()) - pad, float(lat.max()) + pad, float(lon.min()) - pad, float(lon.max()) + pad)


def common_run(sources: tuple[str, ...], steps: list[int], *, sess=None, max_lookback: int = 2,
               min_age_hours: float | None = None) -> object:
    """Newest cycle published by *every* source with enough lead time.

    Sharing a cycle matters: the blend keys on ``step``, and step 24 of an 06Z
    ECMWF run is a different valid time from step 24 of a 12Z GFS run. Picking a
    common cycle keeps ``valid_time`` identical across members.
    """
    sess = sess or session()
    depth = max(steps)
    kwargs = {"max_lookback": max_lookback}
    if min_age_hours is not None:
        kwargs["min_age_hours"] = min_age_hours
    candidates = REGISTRY[sources[0]].candidate_runs(**kwargs)
    for run in candidates:
        if all(REGISTRY[s].probe_run(sess, run, depth) for s in sources):
            return run
    raise RuntimeError(
        f"no cycle in the last {max_lookback + 1} days is published by all of {sources} out to {depth} h"
    )


def run_source(name: str, points: list[Township], steps: list[int], *, pad: float = 0.75,
               cfg: downscale.DownscaleConfig | None = None, sess=None, run=None,
               ) -> tuple[str, xr.Dataset, object]:
    """Fetch one source and take it all the way to township values."""
    mod = REGISTRY[name]
    sess = sess or session()
    run = run or mod.latest_run(sess, min_step=max(steps))
    have = mod.available_steps(sess, run)
    missing = [s for s in steps if s not in have]
    if missing:
        raise ValueError(
            f"{name} run {run}: requested steps {missing} not published "
            f"(available up to {max(have)} h, {_step_hint(have)})"
        )
    lat_min, lat_max, lon_min, lon_max = bbox_of(points, pad)
    if lon_min > lon_max:  # antimeridian: crop after the fact instead
        bbox = None
    else:
        bbox = (lat_min, lat_max, lon_min, lon_max)
    grid = mod.fetch(run, steps, sess=sess, bbox=bbox)
    if bbox is None:
        grid = grid.bbox(lat_min, lat_max, lon_min, lon_max)
    ds = downscale.apply(grid, points, cfg)
    return ds.attrs["source"], ds, run


def _step_hint(steps: list[int]) -> str:
    gap = np.diff(sorted(steps))
    return f"{int(np.median(gap))}-hourly" if len(gap) else "single step"


def forecast(points: list[Township], *, steps: list[int], sources: tuple[str, ...] = ("ecmwf", "gfs"),
             pad: float = 0.75, weights: dict[str, float] | None = None,
             cfg: downscale.DownscaleConfig | None = None, sess=None, run=None) -> dict[str, xr.Dataset]:
    """Return ``{source: township dataset, ..., "blend": ...}``.

    ``steps`` must be lead times both sources publish — with ECMWF IFS at
    3-hourly and GFS hourly, 3-hourly (3, 6, 9, ...) is the useful common grid.
    """
    sess = sess or session()
    if run is None:
        run = common_run(sources, steps, sess=sess) if len(sources) > 1 else None
    per_source: dict[str, xr.Dataset] = {}
    for name in sources:
        key, ds, _run = run_source(name, points, steps, pad=pad, cfg=cfg, sess=sess, run=run)
        per_source[key] = ds
    if len(per_source) > 1:
        per_source["blend"] = blending.combine(per_source, weights)
    return per_source


def step_grid(hours: int, every: int = 3, start: int | None = None) -> list[int]:
    """``[start, start+every, ..., hours]`` — the common step set across sources."""
    start = every if start is None else start
    if hours <= 0 or every <= 0:
        raise ValueError("hours and every must be positive")
    return list(range(start, hours + 1, every))
