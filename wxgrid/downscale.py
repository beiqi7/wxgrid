"""Scale a 0.25 deg model grid down to township point values.

What is defensible, and what is not
-----------------------------------
**Temperature** — elevation is the dominant sub-grid control on 2 m temperature,
and the correction is a physical identity, not a curve fit::

    T_town = T_grid + gamma * (z_town - z_grid)

with ``gamma`` the environmental lapse rate (default -6.5 K/km). After this the
townships in a mountainous county genuinely separate by several kelvin. The
residual (cold-air pooling in valleys, aspect, urban heat) is what a station
calibration removes later.

**Wind** — 10 m wind does *not* follow elevation. A 28 km grid cell reports a
cell-mean wind; a sheltered valley township is calmer and a ridge township is
windier, and the sign of the difference depends on exposure, not height. The
honest default is therefore ``wind_factor = 1.0`` (i.e. assume nothing) and let
:mod:`wxgrid.calibrate` fit a per-township multiplicative factor from station
observations. Reporting a fabricated spread here would be worse than reporting
no spread.

**Precipitation** — same story: the grid mean is a poor proxy for a township in
complex terrain. Default ``precip_factor = 1.0``; a fitted per-township factor
plus a wet-frequency adjustment comes from calibration.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import xarray as xr

from .grid import ForecastGrid
from .points import Township


@dataclass(frozen=True)
class DownscaleConfig:
    """Knobs for :func:`apply`. Defaults are the physically-motivated ones."""

    #: Environmental lapse rate, K per metre. Negative cools with height.
    lapse_rate: float = -0.0065
    #: Hard cap on |elevation correction| so a bad DEM/value cannot explode the field.
    max_correction_k: float = 10.0
    #: Multiplicative wind factors, per township id (default 1.0 = no assumption).
    wind_factor: dict[str, float] | None = None
    #: Multiplicative precipitation factors, per township id (default 1.0).
    precip_factor: dict[str, float] | None = None
    #: Also emit a high-resolution elevation raster alongside the point table.
    emit_raster: bool = False


def _wind_dir(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    return (270.0 - np.degrees(np.arctan2(v, u))) % 360.0


def apply(fg: ForecastGrid, points: list[Township], cfg: DownscaleConfig | None = None) -> xr.Dataset:
    """Return an (point, step) dataset of township values from one model grid."""
    cfg = cfg or DownscaleConfig()
    if not points:
        raise ValueError("no townships given")
    lat = np.array([p.lat for p in points], dtype=float)
    lon = np.array([p.lon for p in points], dtype=float)
    z_town = np.array([p.elevation_m for p in points], dtype=float)

    on_points = fg.interp(lat, lon)

    z_grid = np.asarray(on_points["orog"].values, dtype=float)
    if z_grid.ndim == 1:
        z_grid = z_grid[:, None]
    dz = z_town[:, None] - z_grid
    correction = np.clip(cfg.lapse_rate * dz, -cfg.max_correction_k, cfg.max_correction_k)
    correction = np.broadcast_to(correction, (len(points), len(fg.steps)))

    t2m = on_points["t2m"].values.astype(float) + correction
    u10 = on_points["u10"].values.astype(float)
    v10 = on_points["v10"].values.astype(float)
    wind_speed = np.hypot(u10, v10)
    wind_dir = _wind_dir(u10, v10)

    gust = on_points["gust"].values.astype(float) if "gust" in on_points else None
    if cfg.wind_factor:
        k = np.array([cfg.wind_factor.get(p.id, 1.0) for p in points], dtype=float)[:, None]
        wind_speed = wind_speed * k
        u10, v10 = u10 * k, v10 * k
        if gust is not None:
            gust = gust * k

    if cfg.precip_factor:
        k = np.array([cfg.precip_factor.get(p.id, 1.0) for p in points], dtype=float)[:, None]
    else:
        k = np.ones((len(points), 1))
    tp_cum = on_points["tp"].values.astype(float) * k
    precip = np.clip(np.diff(tp_cum, axis=1, prepend=0.0), 0.0, None)
    cloud = on_points["tcc"].values.astype(float) if "tcc" in on_points else np.zeros_like(tp_cum)
    if np.nanmax(cloud) > 1.0 + 1e-6 or np.nanmin(cloud) < -1e-6:
        raise ValueError(
            f"cloud cover must be a 0-1 fraction, got {np.nanmin(cloud):.2f}..{np.nanmax(cloud):.2f} "
            f"— a source is emitting percent"
        )
    snow_cum = on_points["snow"].values.astype(float) if "snow" in on_points else np.zeros_like(tp_cum)
    snowfall = np.clip(np.diff(snow_cum, axis=1, prepend=0.0), 0.0, None)

    coords = {
        "point": [p.id for p in points],
        "step": fg.steps,
        "valid_time": ("step", on_points["valid_time"].values),
    }
    ds = xr.Dataset(
        {
            "t2m": (("point", "step"), t2m, {"units": "degC", "long_name": "2 m air temperature"}),
            "wind_speed": (("point", "step"), wind_speed, {"units": "m/s"}),
            "wind_dir": (("point", "step"), wind_dir, {"units": "degree", "comment": "direction the wind blows FROM"}),
            "u10": (("point", "step"), u10, {"units": "m/s"}),
            "v10": (("point", "step"), v10, {"units": "m/s"}),
            "precip": (("point", "step"), precip, {"units": "mm", "comment": "increment over the step"}),
            "precip_accum": (("point", "step"), tp_cum, {"units": "mm", "comment": "since model init"}),
            "snow": (("point", "step"), snowfall, {"units": "mm", "comment": "water equivalent"}),
            "cloud": (("point", "step"), cloud, {"units": "1"}),
        },
        coords=coords,
        attrs={
            "source": fg.source,
            "init_time": np.datetime_as_string(np.asarray(fg.init_time, dtype="datetime64[s]"), unit="s"),
            "lapse_rate_k_per_m": cfg.lapse_rate,
        },
    )
    ds = ds.assign_coords(
        latitude=("point", lat),
        longitude=("point", lon),
        elevation=("point", z_town),
        model_elevation=("point", z_grid[:, 0] if z_grid.ndim == 2 else z_grid),
        name=("point", [p.name for p in points]),
    )
    if gust is not None:
        ds["gust"] = xr.DataArray(gust, dims=("point", "step"),
                                  coords={"point": ds["point"], "step": ds["step"]},
                                  attrs={"units": "m/s", "long_name": "10 m wind gust"})
    # Window extremes are temperatures too, so they take the same elevation correction.
    for var in ("tmax3", "tmin3"):
        if var in on_points:
            ds[var] = xr.DataArray(
                on_points[var].values.astype(float) + correction, dims=("point", "step"),
                coords={"point": ds["point"], "step": ds["step"]}, attrs={"units": "degC"},
            )
    ds["elevation_correction"] = xr.DataArray(
        correction, dims=("point", "step"), coords={"point": ds["point"], "step": ds["step"]},
        attrs={"units": "K", "comment": "added to grid t2m"},
    )
    return ds
