"""From numbers to bulletin words: sky, precipitation grade, wind force.

Thresholds follow the national standards, not invented cut-offs:

* 蒲福风级 — 10 m, 10-minute mean wind speed (GB/T 28591-2012).
* 降水量等级 — 12 h and 24 h totals (GB/T 28592-2012). A 白天/夜间 period is
  12 hours, so its word uses the **12 h** grades: 12 mm in one night is 中雨,
  not the 小雨 the 24 h table would give.
* 降雪量等级 — 12 h / 24 h water equivalent, same standard.
* 天空状况 — 晴 总云量 0–3 成, 多云 4–7 成, 阴 8–10 成. The public weather-code
  table (晴/多云/阴/阵雨/…/小雨/中雨/…) has no 少云, so neither does this.

There is no national grade for 1 h or 3 h amounts; :func:`window_text` words those
with the AMS rain-rate classes on the window's mean rate (see ``RATE_CLASSES``).
"""

from __future__ import annotations

import math

#: Upper bound (m/s, inclusive) of Beaufort force 0..11; force 12 is anything above.
BEAUFORT_MAX = (0.2, 1.5, 3.3, 5.4, 7.9, 10.7, 13.8, 17.1, 20.7, 24.4, 28.4, 32.6)

#: (upper bound mm, exclusive, name) — GB/T 28592-2012.
RAIN_24H = ((0.1, "无降水"), (10.0, "小雨"), (25.0, "中雨"), (50.0, "大雨"),
            (100.0, "暴雨"), (250.0, "大暴雨"), (float("inf"), "特大暴雨"))
RAIN_12H = ((0.1, "无降水"), (5.0, "小雨"), (15.0, "中雨"), (30.0, "大雨"),
            (70.0, "暴雨"), (140.0, "大暴雨"), (float("inf"), "特大暴雨"))
SNOW_24H = ((0.1, ""), (2.5, "小雪"), (5.0, "中雪"), (10.0, "大雪"), (20.0, "暴雪"),
            (30.0, "大暴雪"), (float("inf"), "特大暴雪"))
SNOW_12H = ((0.1, ""), (1.0, "小雪"), (3.0, "中雪"), (6.0, "大雪"), (10.0, "暴雪"),
            (15.0, "大暴雪"), (float("inf"), "特大暴雪"))
#: Back-compat names used by older call sites.
RAIN_GRADES, SNOW_GRADES = RAIN_24H, SNOW_24H

#: Rain *rate* words, (upper bound mm/h exclusive, name). AMS observing classes —
#: light <=2.5, moderate 2.6-7.6, heavy >7.6 mm/h — plus the CMA 短时强降水
#: criterion, >=20 mm in one hour.
RATE_CLASSES = ((0.1, ""), (2.6, "小雨"), (7.7, "中雨"), (20.0, "大雨"), (float("inf"), "强降水"))
HOURLY_RAIN = RATE_CLASSES  # name kept for the hourly module

#: Rank of each rain word, for "the heavier of two" and 小到中雨-style ranges.
RAIN_RANK = {"小雨": 1, "中雨": 2, "大雨": 3, "暴雨": 4, "大暴雨": 5, "特大暴雨": 6}

#: 8-point compass, the convention in Chinese public forecasts.
_COMPASS8 = ("北风", "东北风", "东风", "东南风", "南风", "西南风", "西风", "西北风")

#: Mention the gust only from this force up — the level where it matters to the
#: public and where 大风 signals start (阵风 7 级 is 大风蓝色).
GUST_MENTION_FORCE = 6


def beaufort(speed_ms: float) -> int:
    """Beaufort force for a 10 m wind speed in m/s (NaN counts as calm, never as force 12)."""
    if not math.isfinite(speed_ms):
        return 0
    for force, upper in enumerate(BEAUFORT_MAX):
        if speed_ms <= upper:
            return force
    return 12


def wind_name(direction_deg: float) -> str:
    return _COMPASS8[int((direction_deg % 360) / 45.0 + 0.5) % 8]


def force_text(lo: int, hi: int) -> str:
    """``<3级`` / ``3级`` / ``3～4级`` — ranges one force wide, as bulletins write them."""
    if hi <= 2:
        return "<3级"
    if lo >= hi:
        return f"{hi}级"
    return f"{max(lo, hi - 1)}～{hi}级"


def wind_text(direction_deg: float, speed_lo: float, speed_hi: float,
              gust_ms: float | None = None) -> str:
    """``东北风<3级`` / ``东北风3～4级，阵风6级``.

    The force range is the period's lowest and highest mean-wind force; the
    gust is added only when it reaches :data:`GUST_MENTION_FORCE` and is at least
    two forces above the mean wind.
    """
    lo, hi = beaufort(speed_lo), beaufort(speed_hi)
    text = f"{wind_name(direction_deg)}{force_text(lo, hi)}"
    if gust_ms is not None and math.isfinite(gust_ms):
        g = beaufort(gust_ms)
        if g >= GUST_MENTION_FORCE and g >= hi + 2:
            text += f"，阵风{g}级"
    return text


def sky_text(cloud_fraction: float) -> str:
    """总云量 -> 晴 (0-3 成) / 多云 (4-7 成) / 阴 (8-10 成)."""
    if cloud_fraction < 0.35:
        return "晴"
    if cloud_fraction < 0.75:
        return "多云"
    return "阴"


def _grade(mm: float, table) -> str:
    for upper, name in table:
        if mm < upper:
            return name
    return table[-1][1]


def precip_text(mm: float, hours: int = 24) -> str:
    """Rain grade for a 12 h or 24 h total."""
    return _grade(mm, RAIN_12H if hours <= 12 else RAIN_24H)


def snow_text(mm_we: float, hours: int = 24) -> str:
    return _grade(mm_we, SNOW_12H if hours <= 12 else SNOW_24H)


def half_day_text(precip_mm: float, snow_mm: float, cloud_fraction: float, hours: int = 12) -> str:
    """Weather for one 白天/夜间 period: precipitation wins over sky cover.

    Snow is named when it is most of the precipitation; a mix is 雨夹雪.
    """
    if snow_mm >= 0.1:
        rain = precip_mm - snow_mm
        if rain >= 0.1 and rain >= snow_mm * 0.5:
            return "雨夹雪"
        name = snow_text(snow_mm, hours)
        if name:
            return name
    if precip_mm >= 0.1:
        return precip_text(precip_mm, hours)
    return sky_text(cloud_fraction)


def join_halves(day_text: str, night_text: str) -> str:
    """``多云转小雨`` when the two halves differ, otherwise just one word."""
    return day_text if day_text == night_text else f"{day_text}转{night_text}"


def rain_range(a: str, b: str) -> str:
    """``小到中雨`` for two adjacent rain grades, else the heavier one."""
    ra, rb = RAIN_RANK.get(a), RAIN_RANK.get(b)
    if ra is None or rb is None:
        return a if ra else b
    lo, hi = sorted((a, b), key=RAIN_RANK.get)
    if RAIN_RANK[hi] - RAIN_RANK[lo] == 1:
        if hi == "大暴雨":
            return "暴雨到大暴雨"
        if hi == "特大暴雨":
            return "大暴雨到特大暴雨"
        return f"{lo[0]}到{hi}"
    return hi


def window_text(precip_mm: float, snow_mm: float, cloud_fraction: float, hours: int = 3) -> str:
    """Weather for a 1 h or 3 h window, worded by the window's mean rain rate."""
    if snow_mm >= 0.1:
        return "雨夹雪" if precip_mm - snow_mm >= 0.1 else "雪"
    if precip_mm >= 0.1:
        rate = precip_mm / max(hours, 1)
        for upper, name in RATE_CLASSES[1:]:
            if rate < upper:
                return name
        return RATE_CLASSES[-1][1]
    return sky_text(cloud_fraction)


def hour_text(precip_mm: float, snow_mm: float, cloud_fraction: float) -> str:
    """Weather for one hour (see :func:`window_text`)."""
    return window_text(precip_mm, snow_mm, cloud_fraction, hours=1)


def pop_text(pop: float | None) -> str:
    """Round a probability to the 10 % steps forecasts use (detail tables only)."""
    if pop is None:
        return "—"
    return f"{int(round(pop / 10.0) * 10)}%"
