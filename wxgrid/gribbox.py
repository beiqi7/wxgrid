"""Raw model fields cut to a small latitude/longitude box.

This is the data layer of the native engine (:mod:`wxgrid.native`). A global
0.25° field is ~1 M values; the county and the stations it is calibrated
against fit in a box of a few hundred. Each GRIB message is decoded once,
cut to the box and normalised to the units the rest of wxgrid uses, and a run
is kept as a :class:`BoxRun` — small enough (~100 kB) to archive every cycle,
which is what the station calibration is trained on. Live forecasts and the
historical backfill go through the same code, so what is trained on is what
runs.

Canonical fields (any may be missing for a given model):

``t2m``          2 m temperature at the step, ℃
``tmax``/``tmin``  2 m temperature max / min over the window ending at the step, ℃
                 (window start in ``BoxRun.window_start``)
``tp``           precipitation accumulated from init, mm
``snow``         snowfall accumulated from init, mm water equivalent
``u10``/``v10``  10 m wind components, m/s
``gust``         10 m gust, m/s
``tcc``          total cloud cover, 0–1
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import io
import json
import os
import pathlib
import struct
from typing import Iterable

import numpy as np

#: Standard gravity, for geopotential -> metres.
G0 = 9.80665


@dataclasses.dataclass(frozen=True)
class Box:
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    @classmethod
    def around(cls, points: Iterable, pad: float = 0.75) -> "Box":
        """The box around points with ``.lat``/``.lon``, padded by ``pad`` degrees."""
        pts = list(points)
        lat = [float(p.lat) for p in pts]
        lon = [float(p.lon) for p in pts]
        return cls(min(lat) - pad, max(lat) + pad, min(lon) - pad, max(lon) + pad)

    def contains(self, lat: float, lon: float) -> bool:
        return self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max


# ------------------------------------------------------------------ GRIB plumbing

def split_messages(blob: bytes) -> list[bytes]:
    """Concatenated GRIB2 (or GRIB1) messages -> one bytes object per message."""
    out, i, n = [], 0, len(blob)
    while i < n:
        j = blob.find(b"GRIB", i)
        if j < 0:
            break
        edition = blob[j + 7]
        if edition == 2:
            length = struct.unpack(">Q", blob[j + 8:j + 16])[0]
        elif edition == 1:
            length = int.from_bytes(blob[j + 4:j + 7], "big")
        else:
            raise ValueError(f"unknown GRIB edition {edition} at byte {j}")
        if length <= 0 or j + length > n:
            raise ValueError(f"truncated GRIB message at byte {j}: {length} bytes, {n - j} left")
        out.append(blob[j:j + length])
        i = j + length
    return out


def _to_canonical(values: np.ndarray, units: str, field: str) -> np.ndarray:
    """Unit conversion by the message's own ``units`` key.

    ECMWF IFS codes precipitation in metres and AIFS in kg m⁻²; IFS cloud is a
    fraction and AIFS/GFS cloud is percent — trusting a per-model table here
    silently breaks when a producer re-encodes, so the key decides.
    """
    u = (units or "").strip()
    if u == "K":
        return values - 273.15
    if field in ("tp", "snow"):
        if u == "m":
            return values * 1000.0
        return values                       # kg m-2 == mm
    if field == "tcc":
        if u == "%":
            return values / 100.0
        return values                       # (0 - 1)
    if field == "orog":
        if u.startswith("m**2") or u == "m2 s-2" or u == "m**2 s**-2":
            return values / G0
        return values                       # gpm / m
    return values


def decode_box(msg: bytes, box: Box) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict]:
    """One message -> ``(values[lat, lon], lat (descending), lon (ascending), keys)``.

    Handles both longitude conventions (ECMWF open data starts at 180°, NCEP at
    0°) and either latitude scan direction. Values are float64, missing -> NaN.
    """
    import eccodes

    h = eccodes.codes_new_from_message(msg)
    try:
        keys = {k: eccodes.codes_get(h, k) for k in
                ("shortName", "units", "stepRange", "Ni", "Nj", "iDirectionIncrementInDegrees",
                 "jDirectionIncrementInDegrees", "latitudeOfFirstGridPointInDegrees",
                 "longitudeOfFirstGridPointInDegrees", "jScansPositively")}
        if eccodes.codes_get(h, "bitmapPresent"):
            eccodes.codes_set(h, "missingValue", 9.999e20)
        vals = eccodes.codes_get_values(h).astype(float)
        if eccodes.codes_get(h, "bitmapPresent"):
            vals[vals >= 9.99e20] = np.nan
    finally:
        eccodes.codes_release(h)
    ni, nj = int(keys["Ni"]), int(keys["Nj"])
    di, dj = float(keys["iDirectionIncrementInDegrees"]), float(keys["jDirectionIncrementInDegrees"])
    lat0, lon0 = float(keys["latitudeOfFirstGridPointInDegrees"]), float(keys["longitudeOfFirstGridPointInDegrees"])
    field = vals.reshape(nj, ni)
    lat = lat0 + dj * np.arange(nj) * (1 if keys["jScansPositively"] else -1)
    if keys["jScansPositively"]:
        field, lat = field[::-1], lat[::-1]
    rows = np.nonzero((lat >= box.lat_min - 1e-6) & (lat <= box.lat_max + 1e-6))[0]
    lon = (lon0 + di * np.arange(ni) + 180.0) % 360.0 - 180.0   # -180..180, possibly rotated
    cols = np.nonzero((lon >= box.lon_min - 1e-6) & (lon <= box.lon_max + 1e-6))[0]
    if rows.size == 0 or cols.size == 0:
        raise ValueError(f"box {box} selects no grid points")
    cols = cols[np.argsort(lon[cols])]
    return field[np.ix_(rows, cols)], lat[rows], lon[cols], keys


# ------------------------------------------------------------------ container

@dataclasses.dataclass
class BoxRun:
    """One model cycle on a lat/lon box: ``fields[name]`` is ``(step, lat, lon)`` float32."""

    source: str
    init: dt.datetime                     # naive UTC
    steps: np.ndarray                     # int hours after init
    lat: np.ndarray                       # descending
    lon: np.ndarray                       # ascending
    fields: dict[str, np.ndarray]
    orog: np.ndarray | None = None        # model terrain height, m
    window_start: np.ndarray | None = None  # start hour of the tmax/tmin window ending at each step
    meta: dict = dataclasses.field(default_factory=dict)

    @property
    def stamp(self) -> str:
        return self.init.strftime("%Y%m%d%H")

    def save(self, path: str | os.PathLike) -> pathlib.Path:
        path = pathlib.Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        arrays = {"steps": self.steps.astype(np.int16), "lat": self.lat.astype(np.float32),
                  "lon": self.lon.astype(np.float32)}
        arrays.update({f"f_{k}": v.astype(np.float32) for k, v in self.fields.items()})
        if self.orog is not None:
            arrays["orog"] = self.orog.astype(np.float32)
        if self.window_start is not None:
            arrays["window_start"] = self.window_start.astype(np.int16)
        head = {"source": self.source, "init": self.init.isoformat(), "meta": self.meta}
        arrays["head"] = np.frombuffer(json.dumps(head).encode("utf-8"), dtype=np.uint8)
        buf = io.BytesIO()
        np.savez_compressed(buf, **arrays)
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.part")
        tmp.write_bytes(buf.getvalue())
        os.replace(tmp, path)
        return path

    @classmethod
    def load(cls, path: str | os.PathLike) -> "BoxRun":
        with np.load(path) as z:
            head = json.loads(bytes(z["head"]).decode("utf-8"))
            fields = {k[2:]: z[k].astype(float) for k in z.files if k.startswith("f_")}
            return cls(source=head["source"], init=dt.datetime.fromisoformat(head["init"]),
                       steps=z["steps"].astype(int), lat=z["lat"].astype(float), lon=z["lon"].astype(float),
                       fields=fields, orog=z["orog"].astype(float) if "orog" in z.files else None,
                       window_start=z["window_start"].astype(int) if "window_start" in z.files else None,
                       meta=head.get("meta", {}))

    # -------------------------------------------------------------- sampling

    def bilinear(self, lat: np.ndarray, lon: np.ndarray, field: np.ndarray) -> np.ndarray:
        """Bilinear interpolation of ``field[..., lat, lon]`` to points -> ``(..., point)``."""
        la, lo = self.lat[::-1], self.lon                       # ascending for searchsorted
        f = field[..., ::-1, :]
        lat = np.asarray(lat, dtype=float)
        lon = np.asarray(lon, dtype=float)
        if (lat < la[0]).any() or (lat > la[-1]).any() or (lon < lo[0]).any() or (lon > lo[-1]).any():
            raise ValueError("points outside the box")
        i = np.clip(np.searchsorted(la, lat) - 1, 0, la.size - 2)
        j = np.clip(np.searchsorted(lo, lon) - 1, 0, lo.size - 2)
        wy = (lat - la[i]) / (la[i + 1] - la[i])
        wx = (lon - lo[j]) / (lo[j + 1] - lo[j])
        return (f[..., i, j] * (1 - wy) * (1 - wx) + f[..., i + 1, j] * wy * (1 - wx)
                + f[..., i, j + 1] * (1 - wy) * wx + f[..., i + 1, j + 1] * wy * wx)

    def local_lapse(self, radius: int = 2, *, min_relief: float = 150.0,
                    clip: tuple[float, float] = (-0.0098, 0.004)) -> np.ndarray | None:
        """Lapse rate the model itself has, K/m, per ``(step, lat, lon)``.

        A least-squares slope of ``t2m`` against model terrain over the
        ``(2·radius+1)²`` neighbourhood of each grid point. Where the
        neighbourhood is too flat (relief < ``min_relief``) the slope is
        meaningless and NaN is returned there. Clipped to between dry
        adiabatic and a moderate inversion.
        """
        if self.orog is None or "t2m" not in self.fields:
            return None
        t = self.fields["t2m"]
        z = self.orog
        ns, ny, nx = t.shape
        out = np.full(t.shape, np.nan)
        for y in range(ny):
            for x in range(nx):
                y0, y1, x0, x1 = max(0, y - radius), min(ny, y + radius + 1), max(0, x - radius), min(nx, x + radius + 1)
                zz = z[y0:y1, x0:x1].ravel()
                if zz.max() - zz.min() < min_relief:
                    continue
                tt = t[:, y0:y1, x0:x1].reshape(ns, -1)
                zc = zz - zz.mean()
                slope = (tt - tt.mean(axis=1, keepdims=True)) @ zc / (zc @ zc)
                out[:, y, x] = np.clip(slope, *clip)
        return out
