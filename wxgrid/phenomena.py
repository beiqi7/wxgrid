"""From numbers to bulletin words: sky, precipitation grade, wind force.

All thresholds are the CMA / national standard ones, not invented:

* 蒲福风级 — 10 m, 10-minute mean wind speed (GB/T 28591-2012).
* 降水量等级 — 24 h totals (GB/T 28592-2012).
* 降雪量等级 — 24 h water equivalent.
* 云量 — 晴 <3成, 少云 3-5成, 多云 5-8成, 阴 ≥8成.

The one exception is the hourly rain word (:func:`hour_text`): there is no
national 1 h grade, so it uses the AMS rain-rate classes (see HOURLY_RAIN).
"""

from __future__ import annotations

#: Upper bound (m/s, inclusive) of Beaufort force 0..11; force 12 is anything above.
BEAUFORT_MAX = (0.2, 1.5, 3.3, 5.4, 7.9, 10.7, 13.8, 17.1, 20.7, 24.4, 28.4, 32.6)

#: 24 h precipitation grades, (upper bound mm exclusive, name).
RAIN_GRADES = ((0.1, "无降水"), (10.0, "小雨"), (25.0, "中雨"), (50.0, "大雨"),
               (100.0, "暴雨"), (250.0, "大暴雨"), (float("inf"), "特大暴雨"))

SNOW_GRADES = ((0.1, ""), (2.5, "小雪"), (5.0, "中雪"), (10.0, "大雪"), (float("inf"), "暴雪"))

#: 8-point compass, the convention in Chinese public forecasts.
_COMPASS8 = ("北风", "东北风", "东风", "东南风", "南风", "西南风", "西风", "西北风")


def beaufort(speed_ms: float) -> int:
    """Beaufort force for a 10 m wind speed in m/s."""
    for force, upper in enumerate(BEAUFORT_MAX):
        if speed_ms <= upper:
            return force
    return 12


def wind_name(direction_deg: float) -> str:
    return _COMPASS8[int((direction_deg % 360) / 45.0 + 0.5) % 8]


def wind_text(direction_deg: float, speed_lo: float, speed_hi: float,
              gust_ms: float | None = None) -> str:
    """``东北风2～3级`` / ``东北风3级，阵风6级``.

    The bulletin quotes the *range* of force the day reaches, which is what
    "几到几级" means, so it takes the day's lowest and highest mean wind speed.
    """
    lo, hi = beaufort(speed_lo), beaufort(speed_hi)
    force = f"{lo}级" if lo == hi else f"{lo}～{hi}级"
    text = f"{wind_name(direction_deg)}{force}"
    if gust_ms is not None:
        g = beaufort(gust_ms)
        if g >= hi + 2:
            text += f"，阵风{g}级"
    return text


def sky_text(cloud_fraction: float) -> str:
    """云量 -> 晴 / 少云 / 多云 / 阴."""
    if cloud_fraction < 0.30:
        return "晴"
    if cloud_fraction < 0.50:
        return "少云"
    if cloud_fraction < 0.80:
        return "多云"
    return "阴"


def precip_text(mm: float) -> str:
    for upper, name in RAIN_GRADES:
        if mm < upper:
            return name
    return RAIN_GRADES[-1][1]


def snow_text(mm_we: float) -> str:
    for upper, name in SNOW_GRADES:
        if mm_we < upper:
            return name
    return SNOW_GRADES[-1][1]


def half_day_text(precip_mm: float, snow_mm: float, cloud_fraction: float) -> str:
    """Weather for one half of a day: precipitation grade wins over sky cover."""
    if snow_mm >= 0.1 and snow_text(snow_mm):
        return snow_text(snow_mm)
    if precip_mm >= 0.1:
        return precip_text(precip_mm)
    return sky_text(cloud_fraction)


def join_halves(day_text: str, night_text: str) -> str:
    """``多云转小雨`` when the two halves differ, otherwise just one word."""
    return day_text if day_text == night_text else f"{day_text}转{night_text}"


#: Hourly rain-rate words, (upper bound mm/h exclusive, name). GB/T 28592-2012
#: grades only 12 h and 24 h totals, so a 1 h amount is worded with the AMS
#: observing classes instead — light <=2.5, moderate 2.6-7.6, heavy >7.6 mm/h —
#: plus CMA's 短时强降水 criterion, >=20 mm in one hour.
HOURLY_RAIN = ((0.1, ""), (2.6, "小雨"), (7.7, "中雨"), (20.0, "大雨"), (float("inf"), "强降水"))


def hour_text(precip_mm: float, snow_mm: float, cloud_fraction: float) -> str:
    """Weather for one hour: precipitation wins over sky cover."""
    if snow_mm >= 0.1:
        return "雨夹雪" if precip_mm - snow_mm >= 0.1 else "雪"
    if precip_mm >= 0.1:
        for upper, name in HOURLY_RAIN:
            if precip_mm < upper:
                return name
    return sky_text(cloud_fraction)


def pop_text(pop: float | None) -> str:
    """Round a probability to the 10 % steps public forecasts use."""
    if pop is None:
        return "—"
    return f"{int(round(pop / 10.0) * 10)}%"
