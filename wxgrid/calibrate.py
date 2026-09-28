"""Station-based bias correction — the step that turns "plausible" into "usable".

Raw 0.25 deg models carry systematic errors a county office cannot ship:
temperature runs warm or cold by a season-dependent offset, modelled 10 m wind
is too strong in sheltered valleys, and grid-cell precipitation under-reports
convective totals.

The method here is deliberately the transparent one — score against the
automatic weather stations you already operate, fit a *static, per-township*
correction, and keep the raw model alongside the corrected one so the
correction can never hide a model change:

    t2m_corrected  = t2m_model + bias
    wind_corrected = wind_model * wind_factor
    precip_corrected = precip_model * precip_factor

Fitting requires a verification window (a season is the usual minimum) and
``min_obs`` samples per township; anything under that is left uncorrected
rather than fitted on noise.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import xarray as xr

#: Columns :func:`fit` expects in the observations frame.
OBS_COLUMNS = ("point", "valid_time")

#: Clamps that stop a short or skewed window from producing absurd factors.
MAX_FACTOR = 3.0
MIN_FACTOR = 1.0 / 3.0


@dataclass
class Calibration:
    bias_t2m: dict[str, float] = field(default_factory=dict)
    wind_factor: dict[str, float] = field(default_factory=dict)
    precip_factor: dict[str, float] = field(default_factory=dict)
    n_obs: dict[str, int] = field(default_factory=dict)
    window: str = ""

    def to_json(self, path: str | pathlib.Path) -> None:
        pathlib.Path(path).write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def from_json(cls, path: str | pathlib.Path) -> "Calibration":
        return cls(**json.loads(pathlib.Path(path).read_text(encoding="utf-8")))

    def merge(self, other: "Calibration") -> "Calibration":
        return Calibration(
            bias_t2m={**self.bias_t2m, **other.bias_t2m},
            wind_factor={**self.wind_factor, **other.wind_factor},
            precip_factor={**self.precip_factor, **other.precip_factor},
            n_obs={**self.n_obs, **other.n_obs},
            window=other.window or self.window,
        )


def _long(ds: xr.Dataset, variables: tuple[str, ...]) -> pd.DataFrame:
    keep = [v for v in variables if v in ds]
    frame = ds[keep].to_dataframe().reset_index()
    frame = frame[["point", "valid_time", *keep]]
    frame["valid_time"] = pd.to_datetime(frame["valid_time"]).dt.tz_localize(None)
    return frame


def fit(pred: xr.Dataset, obs: pd.DataFrame, *, bias_var: str = "t2m", wind_var: str = "wind_speed",
        precip_var: str = "precip", min_obs: int = 20, max_factor: float = MAX_FACTOR) -> Calibration:
    """Fit per-township corrections from a matched forecast/observation window.

    ``obs`` needs columns ``point``, ``valid_time`` plus any of the modelled
    variable names. Rows are matched on ``(point, valid_time)``.
    """
    missing = [c for c in OBS_COLUMNS if c not in obs.columns]
    if missing:
        raise ValueError(f"observations missing column(s) {missing}")
    obs = obs.copy()
    obs["valid_time"] = pd.to_datetime(obs["valid_time"]).dt.tz_localize(None)

    cal = Calibration(window=f"{obs['valid_time'].min()} .. {obs['valid_time'].max()}")
    merged = _long(pred, (bias_var, wind_var, precip_var)).merge(obs, on=OBS_COLUMNS, suffixes=("", "_obs"))

    for pid, grp in merged.groupby("point", sort=True):
        n = int(len(grp))
        cal.n_obs[str(pid)] = n
        if n < min_obs:
            continue

        if f"{bias_var}_obs" in grp:
            pair = grp[[bias_var, f"{bias_var}_obs"]].dropna()
            if len(pair) >= min_obs:
                cal.bias_t2m[str(pid)] = float((pair[f"{bias_var}_obs"] - pair[bias_var]).mean())

        if f"{wind_var}_obs" in grp:
            pair = grp[[wind_var, f"{wind_var}_obs"]].dropna()
            model_mean = float(pair[wind_var].mean())
            if len(pair) >= min_obs and model_mean > 0.1:
                ratio = float(pair[f"{wind_var}_obs"].mean()) / model_mean
                cal.wind_factor[str(pid)] = float(np.clip(ratio, MIN_FACTOR, max_factor))

        if f"{precip_var}_obs" in grp:
            pair = grp[[precip_var, f"{precip_var}_obs"]].dropna()
            model_sum = float(pair[precip_var].sum())
            if len(pair) >= min_obs and model_sum > 1.0:
                ratio = float(pair[f"{precip_var}_obs"].sum()) / model_sum
                cal.precip_factor[str(pid)] = float(np.clip(ratio, MIN_FACTOR, max_factor))
    return cal


def apply(ds: xr.Dataset, cal: Calibration, *, copy: bool = True) -> xr.Dataset:
    """Apply a calibration, keeping the uncorrected values under ``*_raw``."""
    out = ds.copy(deep=True) if copy else ds
    ids = [str(p) for p in ds["point"].values]

    if cal.bias_t2m:
        b = np.array([cal.bias_t2m.get(p, 0.0) for p in ids])
        out["t2m_raw"] = ds["t2m"]
        out["t2m"] = ds["t2m"] + xr.DataArray(b, dims="point", coords={"point": ds["point"]})

    if cal.wind_factor:
        k = np.array([cal.wind_factor.get(p, 1.0) for p in ids])
        out["wind_speed_raw"] = ds["wind_speed"]
        out["wind_speed"] = ds["wind_speed"] * xr.DataArray(k, dims="point", coords={"point": ds["point"]})
        for comp in ("u10", "v10"):
            if comp in ds:
                out[comp] = ds[comp] * xr.DataArray(k, dims="point", coords={"point": ds["point"]})

    if cal.precip_factor:
        k = np.array([cal.precip_factor.get(p, 1.0) for p in ids])
        out["precip_raw"] = ds["precip"]
        out["precip"] = ds["precip"] * xr.DataArray(k, dims="point", coords={"point": ds["point"]})

    out.attrs = dict(out.attrs)
    out.attrs["calibration"] = cal.window or "none"
    return out


def rmse(pred: xr.Dataset, obs: pd.DataFrame, var: str, *, obs_var: str | None = None) -> float:
    """Point-wise RMSE over the matched window — feed the result to
    :func:`wxgrid.blend.weights_from_skill`."""
    merged = _long(pred, (var,)).merge(obs, on=OBS_COLUMNS, suffixes=("", "_obs"))
    col = obs_var or f"{var}_obs"
    pair = merged[[var, col]].dropna()
    if pair.empty:
        return float("nan")
    return float(np.sqrt(((pair[col] - pair[var]) ** 2).mean()))
