"""Plain-text bulletin, rendered from the product JSON.

Rendering from the JSON (instead of recomputing from the model data) means the
text, the API and the web page cannot disagree. The layout follows a county
bulletin: the county seat period by period (今天夜间 / 明天白天 / …), then one
block per date listing every township. No probabilities appear here — Chinese
public forecasts state the weather, not its odds; the odds stay in the JSON's
detail fields.
"""

from __future__ import annotations

import unicodedata

WEEKDAY = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _width(text: str) -> int:
    """Display columns, counting CJK as two — ``str.ljust`` gets this wrong."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text, width: int, align: str = "<") -> str:
    text = str(text)
    fill = " " * max(0, width - _width(text))
    return fill + text if align == ">" else text + fill


def _md(date: str) -> str:
    return f"{int(date[5:7])}月{int(date[8:10])}日"


def _temp(v) -> str:
    return "—" if v is None else f"{v:.0f}"


def _issue_text(meta: dict) -> str:
    loc = meta.get("issue_local") or ""
    if len(loc) >= 16:
        return f"{int(loc[5:7])}月{int(loc[8:10])}日{loc[11:13]}时{loc[14:16]}分"
    return loc


def seat_block(doc: dict) -> str:
    """The county seat, one line per period."""
    sid = doc["conclusions"].get("seat_id")
    lines = [f"【{doc['meta'].get('seat') or '县城'}】"]
    for q in doc["periods"]:
        c = next((x for x in q["cells"] if x["point"] == sid), q["cells"][0])
        tname = "最高气温" if q["kind"] == "day" else "最低气温"
        rain = f"   降水时段 {c['windows_text']}" if c.get("windows") else ""
        lines.append(f"{_pad(q['label'], 10)}{_pad(c['weather'], 10)}{tname} {_pad(_temp(c['temp']) + '℃', 5)}  "
                     f"{c['wind_text']}{rain}")
    return "\n".join(lines)


def date_blocks(doc: dict) -> str:
    """One block per date, one row per township (白天 转 夜间, 夜间最低～白天最高)."""
    names = {t["id"]: t["name"] for t in doc["townships"]}
    elev = {t["id"]: t["elevation"] for t in doc["townships"]}
    periods = doc["periods"]
    col = (f"{_pad('乡镇', 12)}{_pad('海拔', 6, '>')}  {_pad('天气', 14)}{_pad('气温℃', 9, '>')}  "
           f"{_pad('风', 26)}{_pad('降水mm', 7, '>')}")
    out = []
    for d in doc["days"]:
        day = periods[d["day"]] if d["day"] is not None else None
        night = periods[d["night"]] if d["night"] is not None else None
        which = "白天、夜间" if day and night else ("白天" if day else "夜间")
        out += [f"── {_md(d['date'])} {d['weekday']}（{which}） " + "─" * 10, col, "-" * _width(col)]
        order = sorted(names, key=lambda i: -(elev.get(i) or 0))
        for pid in order:
            cd = next((c for c in day["cells"] if c["point"] == pid), None) if day else None
            cn = next((c for c in night["cells"] if c["point"] == pid), None) if night else None
            wx = (f"{cd['weather']}转{cn['weather']}" if cd and cn and cd["weather"] != cn["weather"]
                  else (cd or cn)["weather"])
            lo = cn["tmin"] if cn else None
            hi = cd["tmax"] if cd else None
            temp = f"{_temp(lo)}～{_temp(hi)}" if cd and cn else (_temp(hi) if cd else _temp(lo))
            if cd and cn and cd["wind_text"] != cn["wind_text"]:
                wind = f"{cd['wind_text']}转{cn['force_text'] if cd['wind_name'] == cn['wind_name'] else cn['wind_text']}"
            else:
                wind = (cd or cn)["wind_text"]
            rain = sum(c["precip"] or 0 for c in (cd, cn) if c)
            out.append(_pad(names[pid], 12) + _pad(f"{elev.get(pid) or 0:.0f}", 6, ">") + "  " + _pad(wx, 14)
                       + _pad(temp, 9, ">") + "  " + _pad(wind, 26) + _pad(f"{rain:.1f}" if rain >= 0.05 else "—", 7, ">"))
        out.append("")
    return "\n".join(out)


def render(doc: dict) -> str:
    m = doc["meta"]
    head = (f"\n{m['county']}未来五天天气预报   （{m['n_townships']}个乡镇）\n"
            f"发布：{_issue_text(m)}（北京时）   起报：{m.get('cycle') or m['run']} UTC   "
            f"成员：{'、'.join(m['member_names']) if m.get('member_names') else m['member']}\n"
            + "═" * 96)
    alerts = doc["conclusions"].get("alerts") or []
    warn = ""
    if alerts:
        rows = []
        for a in alerts:
            lvl = a["level"] if a["level"] == "关注" else f"达{a['level']}预警标准"
            rows.append(f"  {_md(a['date'])} {a['type']}（{lvl}）：{a['detail']}")
        warn = "\n提示（据模式预报，非气象部门发布的预警信号）：\n" + "\n".join(rows) + "\n"
    return "\n".join([head, doc["conclusions"]["headline"], warn, seat_block(doc), "", date_blocks(doc)])
