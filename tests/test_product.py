"""白天/夜间 periods, public wording, and the alert criteria. Offline."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pytest
import xarray as xr

from wxgrid import periods, phenomena, product


# ------------------------------------------------------------- planning

def _plan(init, issue_bjt):
    """Issue time given in Beijing time for readability."""
    issue = dt.datetime.fromisoformat(issue_bjt) - dt.timedelta(hours=8)
    return periods.plan(np.datetime64(init), issue)


def test_morning_issue_opens_with_today_daytime():
    p = _plan("2026-09-28T12:00", "2026-09-29T07:30")         # 12Z run, 07:30 BJT
    assert (p[0].kind, p[0].date, p[0].start_local) == ("day", "2026-09-29", "2026-09-29T08:00")
    assert len(p) == 10 and p[-1].kind == "night" and p[-1].end_lead == 132
    assert [q.kind for q in p] == ["day", "night"] * 5


def test_evening_issue_opens_with_tonight_then_five_full_days():
    p = _plan("2026-09-28T00:00", "2026-09-28T19:30")         # 00Z run, 19:30 BJT
    assert (p[0].kind, p[0].date, p[0].start_lead) == ("night", "2026-09-28", 12)
    assert len(p) == 11 and p[-1].end_lead == 144             # exactly the 3-hourly IFS horizon
    assert p[-1].date == "2026-10-03"


def test_less_than_six_hours_left_skips_to_the_next_period():
    p = _plan("2026-09-28T00:00", "2026-09-28T14:30")
    assert p[0].kind == "night" and p[0].date == "2026-09-28"
    p = _plan("2026-09-28T00:00", "2026-09-28T13:30")
    assert p[0].kind == "day"


def test_a_stale_run_is_cut_at_the_model_horizon_not_padded():
    p = _plan("2026-09-27T00:00", "2026-09-28T19:30")         # a day-old run
    assert p[-1].end_lead <= periods.MAX_LEAD_H
    assert len(p) < 11


def test_steps_start_at_the_first_step_so_precip_increments_are_right():
    p = _plan("2026-09-28T00:00", "2026-09-28T19:30")
    steps = periods.steps_for(p)
    assert steps[0] == 3 and steps[-1] == 144 and np.all(np.diff(steps) == 3)


def test_relative_labels():
    assert periods.relative_label("2026-09-28", "2026-09-28") == "今天"
    assert periods.relative_label("2026-09-30", "2026-09-28") == "后天"
    assert periods.relative_label("2026-10-01", "2026-09-28") == "周四"


def test_after_midnight_the_running_night_is_this_early_morning():
    p = _plan("2026-09-28T00:00", "2026-09-29T01:30")
    assert p[0].kind == "night" and p[0].date == "2026-09-28"
    assert periods.period_label(p[0].date, p[0].kind, "2026-09-29") == "今天凌晨"
    assert periods.period_label("2026-09-29", "day", "2026-09-29") == "今天白天"


# ------------------------------------------------------------- aggregation

def _ds(init="2026-09-28T00:00", steps=range(3, 145, 3), **vals):
    steps = np.asarray(list(steps))
    base = np.datetime64(init, "s")

    def arr(v):
        return np.array([[v(s) if callable(v) else v for s in steps]], float)

    data = {k: (("point", "step"), arr(v)) for k, v in {
        "t2m": 20.0, "tmax3": 21.0, "tmin3": 19.0, "u10": 3.0, "v10": 0.0, "gust": 5.0,
        "precip": 0.0, "snow": 0.0, "cloud": 0.2, **vals}.items()}
    return xr.Dataset(data, coords={
        "point": ["A"], "step": steps, "valid_time": ("step", base + steps.astype("timedelta64[h]")),
        "name": ("point", ["甲镇"]), "latitude": ("point", [28.3]), "longitude": ("point", [117.7]),
        "elevation": ("point", [60.0]), "model_elevation": ("point", [150.0]),
    }, attrs={"init_time": init, "source": "blend(test)"})


def test_day_takes_the_max_and_night_the_min_of_their_own_windows():
    # BJT = step + 8 for a 00Z run. Put a spike in the 17-20 window (step 12) and a dip in 05-08 (step 24).
    ds = _ds(tmax3=lambda s: 35.0 if s == 12 else 25.0, tmin3=lambda s: 10.0 if s == 24 else 18.0)
    plan = _plan("2026-09-28T00:00", "2026-09-28T07:30")      # today day (lead 0-12) + night (12-24)
    agg = periods.aggregate(ds, plan)
    assert plan[0].kind == "day" and agg["tmax"].values[0, 0] == 35.0
    assert plan[1].kind == "night" and agg["tmin"].values[0, 1] == 10.0
    # the 17-20 window is daytime, the 05-08 window is night-time
    assert agg["tmin"].values[0, 0] == 18.0 and agg["tmax"].values[0, 1] == 25.0


def test_period_rain_is_the_sum_of_its_four_windows():
    ds = _ds(precip=lambda s: 2.0 if 15 <= s <= 24 else 0.0)  # windows ending 23,02,05,08 BJT
    plan = _plan("2026-09-28T00:00", "2026-09-28T07:30")
    agg = periods.aggregate(ds, plan)
    assert agg["precip"].values[0, 0] == 0.0 and agg["precip"].values[0, 1] == pytest.approx(8.0)


# ------------------------------------------------------------- wording

@pytest.mark.parametrize("mm,word", [(0.05, "晴"), (0.1, "小雨"), (4.9, "小雨"), (5.0, "中雨"),
                                     (14.9, "中雨"), (15.0, "大雨"), (30.0, "暴雨"), (70.0, "大暴雨")])
def test_periods_use_the_12h_rain_grades(mm, word):
    assert phenomena.half_day_text(mm, 0.0, 0.1) == word


def test_24h_grades_still_exist_for_daily_totals():
    assert phenomena.precip_text(12.0, 24) == "中雨" and phenomena.precip_text(12.0, 12) == "中雨"
    assert phenomena.precip_text(8.0, 24) == "小雨" and phenomena.precip_text(8.0, 12) == "中雨"


def test_snow_grades_and_sleet():
    assert phenomena.half_day_text(1.5, 1.5, 1.0) == "中雪"
    assert phenomena.half_day_text(3.0, 1.0, 1.0) == "雨夹雪"


@pytest.mark.parametrize("cloud,word", [(0.0, "晴"), (0.3, "晴"), (0.4, "多云"), (0.7, "多云"), (0.8, "阴")])
def test_public_sky_words_have_no_shaoyun(cloud, word):
    assert phenomena.sky_text(cloud) == word


def test_wind_wording():
    assert phenomena.wind_text(45, 1.0, 3.0) == "东北风<3级"
    assert phenomena.wind_text(45, 4.0, 6.0) == "东北风3～4级"
    assert phenomena.wind_text(45, 4.0, 4.5) == "东北风3级"
    assert phenomena.wind_text(0, 2.0, 3.0, gust_ms=9.0) == "北风<3级"             # gust 5级: not mentioned
    assert phenomena.wind_text(0, 2.0, 3.0, gust_ms=12.0) == "北风<3级，阵风6级"
    assert phenomena.wind_text(0, 2.0, 3.0, gust_ms=float("nan")) == "北风<3级"  # NaN is not force 12


def test_rain_range_words():
    assert phenomena.rain_range("小雨", "中雨") == "小到中雨"
    assert phenomena.rain_range("暴雨", "大暴雨") == "暴雨到大暴雨"
    assert phenomena.rain_range("小雨", "大雨") == "大雨"


# ------------------------------------------------------------- conclusions

def _doc(ds, issue_bjt="2026-09-28T07:30"):
    issue = dt.datetime.fromisoformat(issue_bjt) - dt.timedelta(hours=8)
    plan = periods.plan(np.datetime64(ds.attrs["init_time"]), issue)
    return product.build(ds, periods=plan, issue_utc=issue, county="测试县", seat="甲镇", run="2026092800",
                         member="blend", sources=("ecmwf", "gfs"), weights=None, tz=8.0)


def _alerts(doc, kind):
    return [a for a in doc["conclusions"]["alerts"] if a["type"] == kind]


def test_three_hour_downpour_reaches_the_orange_rain_standard():
    doc = _doc(_ds(precip=lambda s: 55.0 if s == 30 else 0.0))
    rain = _alerts(doc, "暴雨")
    assert len(rain) == 1 and rain[0]["level"] == "橙色" and "3小时" in rain[0]["criterion"]


def test_steady_rain_over_twelve_hours_reaches_blue():
    doc = _doc(_ds(precip=lambda s: 13.0 if 27 <= s <= 36 else 0.0))   # 52 mm in 12 h, 26 in 6 h
    rain = _alerts(doc, "暴雨")
    assert rain and rain[0]["level"] == "蓝色"


def test_heat_needs_three_days_for_yellow():
    hot = _ds(tmax3=lambda s: 36.0)
    levels = [a["level"] for a in _alerts(_doc(hot), "高温")]
    assert levels[:3] == ["关注", "关注", "黄色"]
    assert _alerts(_doc(_ds(tmax3=lambda s: 38.0)), "高温")[0]["level"] == "橙色"


def test_wind_signal_on_mean_force_or_gust():
    assert _alerts(_doc(_ds(gust=15.0)), "大风")[0]["level"] == "蓝色"   # gust 7级
    assert _alerts(_doc(_ds(u10=18.0, gust=20.0)), "大风")[0]["level"] == "黄色"  # mean 8级
    assert not _alerts(_doc(_ds(gust=12.0)), "大风")                   # gust 6级: below blue


def test_headline_names_the_next_periods_and_has_no_probability():
    doc = _doc(_ds(precip=lambda s: 3.0 if 36 <= s <= 45 else 0.0))
    h = doc["conclusions"]["headline"]
    assert h.startswith("甲镇今天白天晴") and "%" not in h and "概率" not in h
    assert "有小雨" in h or "有中雨" in h


def test_series_3h_starts_after_the_issue_time_and_runs_72_hours():
    doc = _doc(_ds(init="2026-09-27T12:00"))                  # the run a 07:30 issue actually uses
    s = doc["series3h"]
    assert s["times"][0] == "2026-09-28T08:00" and len(s["times"]) == 24
    assert s["lead_h"][0] == 12
    assert s["points"]["A"]["weather"][0] == "晴" and s["points"]["A"]["wind_force"][0] == 2


def test_heavy_rain_probabilities_reach_cells_county_and_api():
    from wxgrid.web.app import township_periods
    ds = _ds(precip=lambda s: 2.0 if 15 <= s <= 24 else 0.0)
    issue = dt.datetime.fromisoformat("2026-09-28T07:30") - dt.timedelta(hours=8)
    plan = periods.plan(np.datetime64(ds.attrs["init_time"]), issue)
    n = len(plan)
    pop = np.full((1, n), 80.0)
    heavy = {"moderate": np.full((1, n), 41.0), "heavy": np.full((1, n), np.nan)}
    doc = product.build(ds, periods=plan, issue_utc=issue, county="测试县", seat="甲镇", run="2026092800",
                        member="native", sources=("ifs",), weights=None, tz=8.0, period_pop=pop,
                        period_pop_heavy=heavy)
    c = doc["periods"][0]["cells"][0]
    assert (c["pop"], c["pop_moderate"], c["pop_heavy"]) == (80, 41, None)
    assert doc["periods"][0]["county"]["pop_moderate_max"] == 41 and doc["periods"][0]["county"]["pop_heavy_max"] is None
    row = township_periods(doc, "甲镇")["periods"][0]
    assert row["pop_moderate"] == 41
    # other engines: the keys exist and are empty
    assert _doc(_ds())["periods"][0]["cells"][0]["pop_moderate"] is None
