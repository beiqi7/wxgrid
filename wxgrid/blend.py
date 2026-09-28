"""Multi-model combination.

ECMWF IFS and GFS are not redundant: different data assimilation, different
physics, different systematic biases. Averaging them buys skill over either
member, but only after each one has been taken to the target points on its own
orography — blending on the native grids first would smear the elevation signal
you just paid for.

Defaults
--------
``DEFAULT_WEIGHTS`` is a prior, not a result. The defensible workflow is:
run both, verify both against local station observations over a rolling window,
then feed :func:`weights_from_skill` the resulting RMSEs.
"""

from __future__ import annotations

import numpy as np
import xarray as xr

#: Prior weighting, ECMWF-heavy. Replace with :func:`weights_from_skill` once
#: you have a verification window from your own stations.
DEFAULT_WEIGHTS = {"ecmwf-ifs-0p25": 0.6, "gfs-0p25": 0.4}

#: Variables blended linearly. Wind direction is re-derived from blended u/v.
_SCALAR = ("t2m", "tmax3", "tmin3", "wind_speed", "gust", "precip", "precip_accum", "snow", "cloud")


def weights_from_skill(rmse: dict[str, float], *, floor: float = 1e-3) -> dict[str, float]:
    """Inverse-variance (1/RMSE^2) weights, normalised to sum to 1."""
    if not rmse:
        raise ValueError("no skill scores given")
    bad = {k: v for k, v in rmse.items() if not np.isfinite(v) or v <= 0}
    if bad:
        raise ValueError(f"non-positive / non-finite RMSE for {sorted(bad)}")
    w = {k: 1.0 / max(v, floor) ** 2 for k, v in rmse.items()}
    total = sum(w.values())
    return {k: v / total for k, v in w.items()}


def combine(per_source: dict[str, xr.Dataset], weights: dict[str, float] | None = None) -> xr.Dataset:
    """Weighted blend of already-downscaled point datasets.

    Every input must share the same ``point`` and ``step`` coordinates; the
    intersection is used.
    """
    if not per_source:
        raise ValueError("nothing to blend")
    weights = dict(weights or DEFAULT_WEIGHTS)
    missing = [k for k in per_source if k not in weights]
    if missing:
        raise KeyError(f"no weight for source(s) {missing}; have {sorted(weights)}")
    used = {k: weights[k] for k in per_source}
    total = sum(used.values())
    if total <= 0:
        raise ValueError(f"weights must be positive, got {used}")
    used = {k: v / total for k, v in used.items()}

    first = next(iter(per_source.values()))
    points = first["point"].values
    steps = first["step"].values
    first_vt = np.asarray(first["valid_time"].values)
    for name, ds in per_source.items():
        if not np.array_equal(ds["point"].values, points) or not np.array_equal(ds["step"].values, steps):
            raise ValueError(f"{name}: point/step coordinates differ from {next(iter(per_source))}")
        # Same step index is not enough — a different cycle means a different valid time.
        if not np.array_equal(np.asarray(ds["valid_time"].values), first_vt):
            raise ValueError(
                f"{name}: valid_time differs from {next(iter(per_source))} — members are on different cycles"
            )

    out_vars: dict[str, tuple] = {}
    for var in _SCALAR:
        if not all(var in ds for ds in per_source.values()):
            continue
        acc = sum(w * per_source[name][var] for name, w in used.items())
        out_vars[var] = acc

    u = sum(w * per_source[name]["u10"] for name, w in used.items())
    v = sum(w * per_source[name]["v10"] for name, w in used.items())
    out_vars["u10"] = u
    out_vars["v10"] = v
    out_vars["wind_speed"] = np.hypot(u, v).rename("wind_speed")
    out_vars["wind_dir"] = ((270.0 - np.degrees(np.arctan2(v, u))) % 360.0).rename("wind_dir")

    ds = xr.Dataset(out_vars, coords=first.coords, attrs=dict(first.attrs))
    ds.attrs.update(
        source="blend(" + "+".join(f"{k}:{w:.2f}" for k, w in used.items()) + ")",
        weights=", ".join(f"{k}={w:.4f}" for k, w in used.items()),
        member_sources=", ".join(sorted(per_source)),
    )
    for c in ("latitude", "longitude", "elevation", "model_elevation", "name"):
        if c in first:
            ds = ds.assign_coords({c: first[c]})
    return ds


def spread(per_source: dict[str, xr.Dataset], var: str) -> xr.DataArray:
    """Max-minus-min across members — a cheap proxy for inter-model disagreement."""
    stack = xr.concat([ds[var] for ds in per_source.values()], dim="member", join="exact")
    stack = stack.assign_coords(member=list(per_source))
    return (stack.max("member") - stack.min("member")).rename(f"{var}_spread")
