"""Text rendering of a township/day forecast — the thing you paste into a bulletin."""

from __future__ import annotations

import numpy as np
import xarray as xr

from .daily import compass


def _fmt_day(day: np.datetime64) -> str:
    d = np.datetime64(day, "D").astype(object)
    return f"{d.month:02d}-{d.day:02d}"


def county_table(daily: xr.Dataset, *, title: str = "", init: str = "") -> str:
    """County-level headline: one line per day, extremes across all townships."""
    from .daily import county_summary

    s = county_summary(daily)
    head = f"{title}  起报 {init}  乡镇数 {daily.sizes['point']}" if title else f"起报 {init}"
    lines = [
        head,
        f"{'日期':<8}{'气温(全县)':<20}{'降水(mm)':<20}{'风速(m/s)':<16}{'覆盖':<6}",
        f"{'':8}{'最低~最高':<20}{'平均 / 最大':<20}{'平均':<16}",
        "-" * 78,
    ]
    for i, day in enumerate(daily["day"].values):
        lines.append(
            f"{_fmt_day(day):<8}"
            f"{s['tmin_min'][i].item():>5.1f}~{s['tmax_max'][i].item():<6.1f}     "
            f"{s['precip_mean'][i].item():>5.1f} / {s['precip_max'][i].item():<6.1f}     "
            f"{s['wind_mean'][i].item():>5.1f} / {s['wind_max'][i].item():<6.1f}   "
            f"{int(daily['hours'][i].item()):>3d}h"
        )
    return "\n".join(lines)


def township_table(daily: xr.Dataset, *, var: str = "tmax", days: int | None = None) -> str:
    """One row per township, one column per day, for a single variable."""
    labels = {
        "tmax": ("最高气温 °C", "{:5.1f}"),
        "tmin": ("最低气温 °C", "{:5.1f}"),
        "tavg": ("平均气温 °C", "{:5.1f}"),
        "precip": ("日降水量 mm", "{:5.1f}"),
        "wind_speed": ("日平均风速 m/s", "{:5.1f}"),
        "wind_speed_max": ("日最大风速 m/s (3小时采样)", "{:5.1f}"),
        "wind_gust": ("日最大阵风 m/s", "{:5.1f}"),
    }
    unit, fmt = labels[var]
    data = daily[var].values
    n = days or data.shape[1]
    names = daily["name"].values
    z = daily["elevation"].values
    header = f"{'乡镇':<12}{'海拔m':>6}  " + "".join(f"{_fmt_day(d):>8}" for d in daily["day"].values[:n])
    rows = [f"— {unit} —", header, "-" * len(header)]
    for i in range(data.shape[0]):
        rows.append(f"{str(names[i]):<12}{z[i]:>6.0f}  "
                    + "".join(f"{fmt.format(data[i, j]):>8}" for j in range(n)))
    return "\n".join(rows)


def wind_day_table(daily: xr.Dataset, *, days: int | None = None) -> str:
    """Wind direction is per-day and per-township, so it gets its own table."""
    n = days or daily.sizes["day"]
    names, z = daily["name"].values, daily["elevation"].values
    dirs, spd = daily["wind_dir"].values, daily["wind_speed"].values
    header = f"{'乡镇':<12}{'海拔m':>6}  " + "".join(f"{_fmt_day(d):>12}" for d in daily["day"].values[:n])
    rows = ["— 主导风向 (矢量平均) / 平均风速 m/s —", header, "-" * len(header)]
    for i in range(len(names)):
        cells = "".join(f"{compass(dirs[i, j]):>6}{spd[i, j]:>6.1f}" for j in range(n))
        rows.append(f"{str(names[i]):<12}{z[i]:>6.0f}  {cells}")
    return "\n".join(rows)


def full_report(daily: xr.Dataset, *, county: str = "", days: int | None = None) -> str:
    n = days or daily.sizes["day"]
    init = str(daily.attrs.get("init_time", ""))
    src = str(daily.attrs.get("source", ""))
    blocks = [
        county_table(daily, title=county, init=init),
        "",
        township_table(daily, var="tmax", days=n),
        "",
        township_table(daily, var="tmin", days=n),
        "",
        township_table(daily, var="precip", days=n),
        "",
        township_table(daily, var="wind_gust", days=n),
        "",
        wind_day_table(daily, days=n),
    ]
    if src:
        blocks.append(f"\n成员: {src}")
    return "\n".join(blocks)
