"""Persist township forecasts to SQLite / NetCDF / Parquet-ready frames."""

from __future__ import annotations

import pathlib
import sqlite3

import pandas as pd
import xarray as xr

#: Column order of the ``forecast`` table written by :func:`write_sqlite`.
COLUMNS = (
    "source", "run_init", "init_time", "step", "valid_time",
    "point", "name", "latitude", "longitude", "elevation", "model_elevation",
    "t2m", "wind_speed", "wind_dir", "precip", "precip_accum",
)


def to_frame(ds: xr.Dataset, source: str | None = None) -> pd.DataFrame:
    """Long frame, one row per (source, township, lead time)."""
    vars_ = [v for v in ("t2m", "wind_speed", "wind_dir", "precip", "precip_accum") if v in ds]
    frame = ds[vars_].to_dataframe().reset_index()
    frame["source"] = source or str(ds.attrs.get("source", "unknown"))
    frame["init_time"] = pd.to_datetime(ds.attrs.get("init_time")).tz_localize(None)
    frame["valid_time"] = pd.to_datetime(frame["valid_time"]).dt.tz_localize(None)
    frame["run_init"] = frame["init_time"].dt.strftime("%Y%m%d%H")
    frame["step"] = frame["step"].astype(int)
    keep = [c for c in COLUMNS if c in frame.columns]
    return frame[keep].sort_values(["point", "step"]).reset_index(drop=True)


def write_sqlite(frames: list[pd.DataFrame], path: str | pathlib.Path, *, table: str = "forecast",
                 replace: bool = True) -> int:
    """Upsert the frames into one table. Returns rows written."""
    if not frames:
        raise ValueError("no frames to write")
    df = pd.concat(frames, ignore_index=True)
    with sqlite3.connect(path) as con:
        df.to_sql(table, con, if_exists="replace" if replace else "append", index=False)
        con.execute(f"CREATE INDEX IF NOT EXISTS ix_{table}_lookup ON {table}(point, valid_time, source)")
    return len(df)


def write_netcdf(per_source: dict[str, xr.Dataset], path: str | pathlib.Path) -> None:
    """One file, one ``member`` dimension holding each source plus the blend."""
    first = next(iter(per_source.values()))
    stack = xr.concat(
        [ds.drop_vars([c for c in ds.coords if c not in ds.dims], errors="ignore") for ds in per_source.values()],
        dim="member", join="exact",
    ).assign_coords(member=list(per_source))
    # These coords are identical across members (the blend copies the first's),
    # so re-attach them once instead of dropping them — `valid_time` in particular
    # is what `to_frame`/`calibrate` read back out of the file.
    for c in ("valid_time", "latitude", "longitude", "elevation", "model_elevation", "name"):
        if c in first.coords:
            stack = stack.assign_coords({c: first[c]})
    p = pathlib.Path(path)
    if p.exists():
        p.unlink()
    stack.to_netcdf(p)
