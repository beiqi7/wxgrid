"""Bulletin rendering: a county-seat forecast and a per-township table.

The county-seat block is the one a duty forecaster actually reads out; the
township tables are the attachment. Both are driven off the same daily and
half-day aggregates, so they can never disagree.
"""

from __future__ import annotations

import datetime as dt
import unicodedata

import numpy as np
import xarray as xr

from . import daily as daily_mod, phenomena

WEEKDAY = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

#: 降水概率 is P(daily precipitation >= this), the CMA criterion, and the same
#: cut is used for the "is there a rain window" question — one criterion, one
#: meaning, so the two columns can never contradict each other.
TRACE_MM = 0.1


def _width(text: str) -> int:
    """Display columns, counting CJK as two — ``str.ljust`` gets this wrong."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int, align: str = "<") -> str:
    text = str(text)
    fill = " " * max(0, width - _width(text))
    return fill + text if align == ">" else text + fill


def _timing(spans, pop: float | None) -> str:
    """Rain window, or an honest statement that it is scattered/absent."""
    if spans:
        return daily_mod.format_windows(spans)
    if pop is not None and pop >= 30:
        return "零星，无集中时段"
    return "无"


def _date_label(day: np.datetime64) -> str:
    d = day.astype("datetime64[D]").astype(dt.date)
    return f"{d.month}月{d.day}日 {WEEKDAY[d.weekday()]}"


def _beijing(ts: np.datetime64, tz_hours: float = 8.0) -> str:
    local_h = int(np.asarray(ts).astype("datetime64[h]").astype("int64")) + int(tz_hours)
    d = np.datetime64(local_h // 24, "D").astype(dt.date)
    return f"{d.month}月{d.day}日{local_h % 24:02d}时"


def seat_forecast(daily: xr.Dataset, halves: xr.Dataset, seat: str, *,
                  pop: xr.DataArray | None = None,
                  windows: dict | None = None, days: int = 5, tz: float = 8.0) -> str:
    """The 河口镇-style block: one line per day, all four questions answered."""
    names = [str(n) for n in daily["name"].values]
    if seat not in names:
        raise KeyError(f"{seat!r} is not in the township table; have {names}")
    i = names.index(seat)
    pid = str(daily["point"].values[i])
    labels = [str(np.datetime64(d, "D")) for d in daily["day"].values]
    pop_ids = [str(p) for p in pop["point"].values] if pop is not None else None

    lines = []
    for k in range(min(days, daily.sizes["day"])):
        day = daily["day"].values[k]
        label = labels[k]
        day_p = float(halves["day_precip"].values[i, k])
        night_p = float(halves["night_precip"].values[i, k])
        day_s = float(halves["day_snow"].values[i, k])
        night_s = float(halves["night_snow"].values[i, k])
        text = phenomena.join_halves(
            phenomena.half_day_text(day_p, day_s, float(halves["day_cloud"].values[i, k])),
            phenomena.half_day_text(night_p, night_s, float(halves["night_cloud"].values[i, k])),
        )
        tmin = float(daily["tmin"].values[i, k])
        tmax = float(daily["tmax"].values[i, k])
        wind = phenomena.wind_text(
            float(daily["wind_dir"].values[i, k]),
            float(daily["wind_speed_min"].values[i, k]),
            float(daily["wind_speed_max"].values[i, k]),
            float(daily["wind_gust"].values[i, k]),
        )
        p = None
        if pop is not None and pop_ids and pid in pop_ids:
            p = float(pop.values[pop_ids.index(pid), k])
        spans = (windows or {}).get((pid, label))
        coverage = int(daily["hours"].values[k])
        suffix = "" if coverage >= 24 else f"（{coverage}小时）"
        lines.append(
            f"{_date_label(day):<14} {_pad(text, 10)} {tmin:>3.0f}～{tmax:<3.0f}℃  "
            f"{_pad(wind, 18)} 降水概率 {phenomena.pop_text(p):>4}   "
            f"降水时段 {_timing(spans, p)}{suffix}"
        )
    return "\n".join(lines)


def day_blocks(daily: xr.Dataset, halves: xr.Dataset, *, pop: xr.DataArray | None = None,
               windows: dict | None = None, days: int = 5) -> str:
    """One block per day, one row per township — reads like a paper bulletin.

    Transposed on purpose: a township-per-row / day-per-column grid needs a
    170-character line for five days and wraps in every terminal.
    """
    names, z = daily["name"].values, daily["elevation"].values
    ids = [str(p) for p in daily["point"].values]
    pop_ids = [str(p) for p in pop["point"].values] if pop is not None else []
    col = (f"{_pad('乡镇', 11)}{_pad('海拔', 6, '>')}  {_pad('天气', 10)}"
           f"{_pad('气温℃', 9, '>')}  {_pad('风', 17)}{_pad('降水概率', 9, '>')}  {_pad('降水时段', 14)}")
    out = []
    for k in range(min(days, daily.sizes["day"])):
        day = daily["day"].values[k]
        coverage = int(daily["hours"].values[k])
        note = "" if coverage >= 24 else f"   （仅覆盖{coverage}小时）"
        out += [f"── {_date_label(day)}{note} " + "─" * 8, col, "-" * len(col)]
        for i, name in enumerate(names):
            text = phenomena.join_halves(
                phenomena.half_day_text(float(halves["day_precip"].values[i, k]),
                                        float(halves["day_snow"].values[i, k]),
                                        float(halves["day_cloud"].values[i, k])),
                phenomena.half_day_text(float(halves["night_precip"].values[i, k]),
                                        float(halves["night_snow"].values[i, k]),
                                        float(halves["night_cloud"].values[i, k])),
            )
            wind = phenomena.wind_text(float(daily["wind_dir"].values[i, k]),
                                       float(daily["wind_speed_min"].values[i, k]),
                                       float(daily["wind_speed_max"].values[i, k]),
                                       float(daily["wind_gust"].values[i, k]))
            p = None
            if pop is not None and ids[i] in pop_ids:
                p = float(pop.values[pop_ids.index(ids[i]), k])
            spans = (windows or {}).get((ids[i], str(np.datetime64(day, "D"))))
            out.append(
                _pad(str(name), 11) + _pad(f"{z[i]:.0f}", 6, ">") + "  "
                + _pad(text, 10)
                + _pad(f"{float(daily['tmin'].values[i, k]):.0f}～{float(daily['tmax'].values[i, k]):.0f}", 9, ">")
                + "  " + _pad(wind, 17)
                + _pad(phenomena.pop_text(p), 9, ">") + "  "
                + _pad(_timing(spans, p), 14)
            )
        out.append("")
    return "\n".join(out)


def header(county: str, run: str, init: np.datetime64, n_points: int, n_days: int,
           member: str, tz: float = 8.0) -> str:
    return (
        f"\n{county}未来{n_days}天天气预报   （{n_points}个乡镇）\n"
        f"起报：{run}  UTC  /  北京时 {_beijing(init, tz)}      成员：{member}\n"
        f"天气现象/气温/风：确定性成员（ECMWF IFS + GFS 加权）；"
        f"降水概率：集合成员中日降水量≥{TRACE_MM:g}mm 的比例\n"
        + "═" * 104
    )
