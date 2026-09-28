"""Elevation from the Copernicus DEM GLO-30 COGs on AWS Open Data.

Tile = one 1x1 degree cell, 3600x3600 at ~30 m (0.0002777 deg), int16 metres,
named by its *south-west* corner::

    https://copernicus-dem-30m.s3.amazonaws.com/
        Copernicus_DSM_COG_10_N28_00_E120_00_DEM/Copernicus_DSM_COG_10_N28_00_E120_00_DEM.tif

Elevation is what makes township-level separation possible at all: a 0.25 deg
model grid ( ~28 km ) holds 60-100 m of unresolved relief in a county like
Jinyun, which is worth several kelvin of 2 m temperature.
"""

from __future__ import annotations

import os
import pathlib
import threading

import numpy as np
import requests
import xarray as xr
from rasterio.warp import Resampling, reproject
from rasterio.windows import from_bounds
from rasterio.transform import from_origin

try:  # shapely >= 2.0
    from shapely import contains_xy as _contains_xy
except ImportError:  # pragma: no cover - shapely 1.8 fallback
    from shapely.vectorized import contains as _contains_xy

BUCKET = "https://copernicus-dem-30m.s3.amazonaws.com"
NATIVE_RES = 1.0 / 3600.0

_TILE_LOCK = threading.Lock()


def tile_name(lat_floor: int, lon_floor: int) -> str:
    ns = "N" if lat_floor >= 0 else "S"
    ew = "E" if lon_floor >= 0 else "W"
    return f"Copernicus_DSM_COG_10_{ns}{abs(lat_floor):02d}_00_{ew}{abs(lon_floor):03d}_00_DEM"


def tile_url(lat_floor: int, lon_floor: int) -> str:
    name = tile_name(lat_floor, lon_floor)
    return f"{BUCKET}/{name}/{name}.tif"


class CopernicusDEM:
    """Reads 30 m elevation for a bounding box, with an on-disk tile cache."""

    def __init__(self, cache_dir: str | os.PathLike | None = None) -> None:
        self.cache_dir = pathlib.Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    # -- tiles ------------------------------------------------------------
    def _tile_source(self, lat_floor: int, lon_floor: int) -> str:
        url = tile_url(lat_floor, lon_floor)
        if not self.cache_dir:
            return url
        path = self.cache_dir / f"{tile_name(lat_floor, lon_floor)}.tif"
        if not path.exists():
            with _TILE_LOCK:
                if not path.exists():
                    with requests.get(url, stream=True, timeout=120) as r:
                        r.raise_for_status()
                        tmp = path.with_suffix(".part")
                        with open(tmp, "wb") as fh:
                            for chunk in r.iter_content(1 << 20):
                                fh.write(chunk)
                        tmp.replace(path)
        return str(path)

    @staticmethod
    def _tiles_for(lat_min: float, lat_max: float, lon_min: float, lon_max: float):
        for lat in range(int(np.floor(lat_min)), int(np.floor(lat_max)) + 1):
            for lon in range(int(np.floor(lon_min)), int(np.floor(lon_max)) + 1):
                yield lat, lon

    # -- reads ------------------------------------------------------------
    def mosaic(self, lat_min: float, lat_max: float, lon_min: float, lon_max: float,
               res_deg: float | None = None) -> xr.DataArray:
        """Elevation over a box, optionally resampled to ``res_deg`` (one cell per pixel)."""
        import rasterio

        target_res = res_deg or NATIVE_RES
        out_lat = np.arange(np.ceil(lat_min / target_res) * target_res,
                            lat_max + target_res / 2, target_res)[::-1]
        out_lon = np.arange(np.floor(lon_min / target_res) * target_res,
                            lon_max + target_res / 2, target_res)
        dst = np.full((out_lat.size, out_lon.size), np.nan, dtype="float32")
        dst_transform = from_origin(out_lon[0] - target_res / 2, out_lat[0] + target_res / 2, target_res, target_res)

        for lat_f, lon_f in self._tiles_for(lat_min, lat_max, lon_min, lon_max):
            with rasterio.open(self._tile_source(lat_f, lon_f)) as src:
                w = from_bounds(lon_min, lat_min, lon_max, lat_max, src.transform).intersection(
                    rasterio.windows.Window(0, 0, src.width, src.height))
                if w.width <= 0 or w.height <= 0:
                    continue
                block = src.read(1, window=w)
                src_transform = src.window_transform(w)
                tile = np.full_like(dst, np.nan)
                reproject(
                    source=block, destination=tile,
                    src_transform=src_transform, src_crs=src.crs,
                    dst_transform=dst_transform, dst_crs=src.crs,
                    resampling=Resampling.average, src_nodata=None, dst_nodata=np.nan,
                )
                # each tile only owns part of dst; keep pixels already filled
                dst = np.where(np.isnan(tile), dst, tile)
        da = xr.DataArray(dst, dims=("latitude", "longitude"), name="elevation",
                          coords={"latitude": np.round(out_lat, 8), "longitude": np.round(out_lon, 8)},
                          attrs={"units": "m", "source": "Copernicus DEM GLO-30"})
        return da

    def point(self, lat: float, lon: float) -> float:
        return float(self.area_mean(lat, lon, 0.0))

    def area_mean(self, lat: float, lon: float, radius_deg: float,
                  polygon=None, res_deg: float = NATIVE_RES) -> float:
        """Mean elevation near a point, or the areal mean inside ``polygon`` if given."""
        import rasterio

        lat_f, lon_f = int(np.floor(lat)), int(np.floor(lon))
        with rasterio.open(self._tile_source(lat_f, lon_f)) as src:
            if polygon is not None:
                b = polygon.bounds
                win = from_bounds(max(b[0], lon_f), max(b[1], lat_f),
                                  min(b[2], lon_f + 1), min(b[3], lat_f + 1), src.transform).intersection(
                    rasterio.windows.Window(0, 0, src.width, src.height))
            else:
                win = from_bounds(lon - radius_deg, lat - radius_deg,
                                  lon + radius_deg, lat + radius_deg, src.transform).intersection(
                    rasterio.windows.Window(0, 0, src.width, src.height))
            if win.width < 1 or win.height < 1:
                r, c = src.index(lon, lat)
                return float(src.read(1, window=((r, 1), (c, 1)))[0, 0])
            block = src.read(1, window=win).astype("float64")
            transform = src.window_transform(win)

        if polygon is None:
            return float(np.nanmean(block))

        rows, cols = block.shape
        rr, cc = np.mgrid[0:rows, 0:cols]
        xs, ys = transform * (cc + 0.5, rr + 0.5)
        mask = _contains_xy(polygon, xs.ravel(), ys.ravel()).reshape(block.shape)
        return float(np.nanmean(block[mask])) if mask.any() else float(np.nanmean(block))


def openmeteo_elevation(lats, lons) -> np.ndarray:
    """Key-free fallback (~90 m GLO-90). Batches of 100, as the API requires."""
    lats, lons = np.atleast_1d(np.asarray(lats, float)), np.atleast_1d(np.asarray(lons, float))
    out = np.empty(lats.size)
    for i in range(0, lats.size, 100):
        sl = slice(i, i + 100)
        r = requests.get(
            "https://api.open-meteo.com/v1/elevation",
            params={"latitude": ",".join(f"{v:.5f}" for v in lats[sl]),
                    "longitude": ",".join(f"{v:.5f}" for v in lons[sl])},
            timeout=30,
        )
        r.raise_for_status()
        out[sl] = r.json()["elevation"]
    return out
