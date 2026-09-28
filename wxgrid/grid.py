"""Canonical forecast container and geometric helpers."""

from __future__ import annotations

import numpy as np
import xarray as xr

#: Variables every source must supply after normalisation.
REQUIRED = ("t2m", "u10", "v10", "tp")

#: WGS-84 mean radius, metres — used only for great-circle point distances.
_EARTH_R = 6371008.8


class ForecastGrid:
    """A normalised forecast: one variable set on (step, latitude, longitude)."""

    __slots__ = ("ds", "source")

    def __init__(self, ds: xr.Dataset, source: str) -> None:
        missing = [v for v in REQUIRED if v not in ds]
        if missing:
            raise ValueError(f"{source}: missing normalised variables {missing}")
        if ds["latitude"][0] < ds["latitude"][-1]:
            raise ValueError(f"{source}: latitude must be descending (N->S)")
        if ds["longitude"][0] > ds["longitude"][-1]:
            raise ValueError(f"{source}: longitude must be ascending (-180..180)")
        self.ds = ds
        self.source = source

    # -- introspection ----------------------------------------------------
    @property
    def steps(self) -> list[int]:
        return [int(s) for s in self.ds["step"].values]

    @property
    def init_time(self) -> np.datetime64:
        return self.ds.attrs["init_time"]

    @property
    def resolution_deg(self) -> float:
        return float(abs(self.ds["latitude"].values[1] - self.ds["latitude"].values[0]))

    def __repr__(self) -> str:  # pragma: no cover - display only
        return (
            f"<ForecastGrid {self.source} init={self.init_time} "
            f"steps={len(self.steps)} res={self.resolution_deg:g}deg>"
        )

    # -- transforms -------------------------------------------------------
    def bbox(self, lat_min: float, lat_max: float, lon_min: float, lon_max: float) -> "ForecastGrid":
        """Crop to a bounding box (degrees east/north). Handles antimeridian wrap."""
        ds = self.ds
        if lon_min <= lon_max:
            d = ds.sel(latitude=slice(lat_max, lat_min), longitude=slice(lon_min, lon_max))
        else:  # crosses 180E — roll the grid so the window is contiguous
            lon = ds["longitude"].values
            rolled = np.where(lon < 0, lon + 360, lon)
            ds = ds.assign_coords(longitude=rolled).sortby("longitude")
            d = ds.sel(latitude=slice(lat_max, lat_min), longitude=slice(lon_min + 360, lon_max + 360))
            d = d.assign_coords(longitude=(((d["longitude"] + 180) % 360) - 180)).sortby("longitude")
        if d.sizes["latitude"] == 0 or d.sizes["longitude"] == 0:
            raise ValueError("bbox selects no grid points — check N/S/E/W ordering")
        out = ForecastGrid.__new__(ForecastGrid)
        out.ds, out.source = d, self.source
        return out

    def step_subset(self, steps: list[int]) -> "ForecastGrid":
        have = set(self.steps)
        want = sorted({int(s) for s in steps})
        missing = [s for s in want if s not in have]
        if missing:
            raise ValueError(f"{self.source}: requested steps unavailable: {missing}")
        out = ForecastGrid.__new__(ForecastGrid)
        out.ds, out.source = self.ds.sel(step=want), self.source
        return out

    # -- derived ----------------------------------------------------------
    def wind_speed(self) -> xr.DataArray:
        return np.hypot(self.ds["u10"], self.ds["v10"]).rename("wind_speed")

    def wind_dir(self) -> xr.DataArray:
        """Meteorological direction the wind blows FROM, degrees clockwise from north."""
        d = (270.0 - np.degrees(np.arctan2(self.ds["v10"], self.ds["u10"]))) % 360.0
        return d.rename("wind_dir")

    def precip_interval(self) -> xr.DataArray:
        """Per-step precipitation increment (mm) from the since-init accumulation."""
        tp = self.ds["tp"]
        step_h = self.ds["step"].values.astype("timedelta64[h]").astype(int)
        inc = tp.diff("step")
        first = tp.isel(step=slice(0, 1))
        return xr.concat([first, inc], dim="step").assign_coords(step=step_h).rename("precip")

    def interp(self, lat: np.ndarray, lon: np.ndarray) -> xr.Dataset:
        """Bilinear-interpolate the model grid onto arbitrary points.

        Returns a Dataset with dim ``point``. Output is *grid-scale* — apply
        :mod:`wxgrid.downscale` before calling it a township value.
        """
        lat_da = xr.DataArray(np.asarray(lat, dtype=float), dims="point", name="latitude")
        lon_da = xr.DataArray(np.asarray(lon, dtype=float), dims="point", name="longitude")
        ds = self.ds
        if float(lon_da.min()) < float(ds["longitude"].min()) or float(lon_da.max()) > float(ds["longitude"].max()):
            lon = np.asarray(lon, dtype=float)
            rolled = np.where(lon < 0, lon + 360, lon)
            ds = ds.assign_coords(longitude=np.where(ds["longitude"].values < 0,
                                                     ds["longitude"].values + 360,
                                                     ds["longitude"].values)).sortby("longitude")
            lon_da = xr.DataArray(rolled, dims="point", name="longitude")
        drop = [c for c in ds.coords if c not in ds["t2m"].dims and c not in ("step", "valid_time")]
        out = ds.drop_vars(drop, errors="ignore").interp(latitude=lat_da, longitude=lon_da, method="linear")
        # xarray keeps the original dim order, i.e. (step, point); callers want (point, step)
        for name, var in list(out.data_vars.items()):
            if set(var.dims) == {"point", "step"}:
                out[name] = var.transpose("point", "step")
        out.attrs.update(ds.attrs)
        return out


def haversine_km(lat1, lon1, lat2, lon2) -> np.ndarray:
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp, dl = p2 - p1, np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return 2 * _EARTH_R * np.arcsin(np.sqrt(a)) / 1000.0
