"""Surface observations from national SYNOP stations, for verification and bias correction.

Source: OGIMET's ``getsynop`` service, which relays the WMO SYNOP bulletins
(3-hourly, ~2 months per request). Chinese stations report, besides the
instantaneous state, rolling extremes and totals that line up with how CMA
verifies its forecasts:

* section 333 ``1sTxTxTx`` / ``2sTnTnTn`` — maximum / minimum temperature over
  the **preceding 24 h**. At 12 UTC that is 20:00–20:00 Beijing time, the window
  CMA uses for 日最高 / 日最低气温.
* section 1 ``6RRRt`` — precipitation over the preceding 6 h (``t`` = 1), sent
  at every 3-hourly report. The 00/06/12/18 UTC ones tile the day, so
  白天 (00–12 UTC) = P6(06) + P6(12) and 夜间 (12–00 UTC) = P6(18) + P6(00).
* section 333 ``7RRRR`` — precipitation over the preceding 24 h in 0.1 mm.
* section 1 ``Nddff`` — 10-minute mean wind (m/s when ``iw`` is 0/1);
  section 333 ``911ff`` — maximum gust in the period given by ``907tt``.

Chinese automatic stations do not report cloud amount (``N`` = ``/``), so sky
cover is not verifiable here.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import pathlib
import time
from typing import Iterable

import numpy as np

from .sources._fetch import get, session

OGIMET = "https://www.ogimet.com/cgi-bin/getsynop"
#: OGIMET asks for gentle use; one request per station per this many seconds.
POLITE_S = 6.0


@dataclasses.dataclass(frozen=True)
class Station:
    wmo: str
    name: str
    lat: float
    lon: float
    elevation_m: float

    @property
    def id(self) -> str:
        return f"WMO{self.wmo}"


#: National stations around 铅山县 that exchange SYNOP internationally
#: (coordinates and barometer elevations from NOAA's ISD station history).
#: The county's own neighbour 上饶 (58626) is not exchanged.
NEAR_YANSHAN = (
    Station("58730", "武夷山", 27.767, 118.033, 221.0),
    Station("58725", "邵武", 27.333, 117.467, 219.0),
    Station("58731", "浦城", 27.917, 118.533, 275.0),
    Station("58715", "南城", 27.583, 116.650, 82.0),
    Station("58527", "景德镇", 29.300, 117.200, 60.0),
    Station("58633", "衢州", 28.967, 118.867, 71.0),
    Station("58606", "南昌", 28.865, 115.900, 44.0),
    Station("58506", "庐山", 29.583, 115.983, 1165.0),
)


# ------------------------------------------------------------------ decoding

def _temp(group: str) -> float | None:
    """``1sTTT`` style: sign digit then tenths of a degree."""
    s, v = group[1], group[2:5]
    if "/" in v or s not in "01":
        return None
    t = int(v) / 10.0
    return -t if s == "1" else t


def _rrr(code: str) -> float | None:
    """WMO code table 3590: mm, 990 = trace (returned as 0.05), 991–999 = 0.1–0.9."""
    if "/" in code:
        return None
    n = int(code)
    if n == 990:
        return 0.05
    if n >= 991:
        return (n - 990) / 10.0
    return float(n)


#: code table 4019, tR -> hours
_TR_HOURS = {"1": 6, "2": 12, "3": 18, "4": 24, "5": 1, "6": 2, "7": 3, "8": 9, "9": 15}


def decode(report: str) -> dict | None:
    """One ``AAXX`` land report -> a flat dict; ``None`` if it is not decodable.

    Only what verification needs is extracted: t, td, wind, 6-hour precipitation,
    24-hour extremes and total, and the hourly max gust.
    """
    body = report.strip().rstrip("=").split()
    if len(body) < 4 or body[0] != "AAXX":
        return None
    yyggi, station = body[1], body[2]
    if len(yyggi) != 5 or not station.isdigit():
        return None
    iw = yyggi[4]
    toks = body[3:]
    out: dict = {"wmo": station}
    if len(toks) < 2:
        return None
    irixhvv, nddff = toks[0], toks[1]
    ir = irixhvv[0] if irixhvv else "/"
    dd, ff = nddff[1:3], nddff[3:5]
    if "/" not in dd and "/" not in ff:
        speed = float(ff)
        if iw in "34":
            speed *= 0.514444
        out["wind_dir"] = None if dd in ("00", "99") else int(dd) * 10.0
        out["wind_speed"] = 0.0 if dd == "00" else speed
    i = 2
    if i < len(toks) and toks[i].startswith("00"):  # ff >= 99
        i += 1
    sec3 = False
    p6_seen = False
    last_digit = 0
    while i < len(toks):
        g = toks[i]
        i += 1
        if g in ("333", "555"):
            if g == "555":
                break
            sec3, last_digit = True, 0
            continue
        if len(g) != 5:
            continue
        d = g[0]
        if not sec3:
            if d == "1":
                out["t"] = _temp(g)
            elif d == "2" and g[1] in "01":
                out["td"] = _temp(g)
            elif d == "6":
                hours = _TR_HOURS.get(g[4])
                amt = _rrr(g[1:4])
                if hours == 6:
                    out["p6"] = amt
                    p6_seen = True
                elif hours:
                    out[f"p{hours}"] = amt
            elif d == "7":
                out["ww"] = None if "/" in g[1:3] else int(g[1:3])
        else:
            # section 3 groups come in increasing first digit; 9-groups repeat
            if d == "1" and last_digit < 1:
                out["tx24"] = _temp(g)
            elif d == "2" and last_digit < 2:
                out["tn24"] = _temp(g)
            elif d == "6" and last_digit < 6:
                # Some stations send the 12 h amount in section 1 and the 6 h one
                # here (``333 ... 6RRR1``); without this their p6 was lost.
                hours = _TR_HOURS.get(g[4])
                amt = _rrr(g[1:4])
                if hours == 6:
                    out["p6"] = amt
                    p6_seen = True
                elif hours:
                    out.setdefault(f"p{hours}", amt)
            elif d == "7" and last_digit < 7:
                v = g[1:5]
                if "/" not in v:
                    out["r24"] = 0.05 if v == "9999" else int(v) / 10.0
            elif d == "9":
                if g.startswith("911") and "/" not in g[3:5]:
                    v = float(g[3:5])
                    out["gust"] = v * 0.514444 if iw in "34" else v
            last_digit = int(d) if d.isdigit() else last_digit
    if not p6_seen:
        if ir == "3":
            out["p6"] = 0.0     # "omitted: no precipitation"
        # ir == 4: not observed -> leave missing
    return out


def parse_ogimet(text: str) -> list[dict]:
    """``getsynop`` CSV lines -> decoded records with a UTC ``time``."""
    rows = []
    for line in text.splitlines():
        parts = line.split(",", 6)
        if len(parts) < 7 or not parts[0].isdigit():
            continue
        try:
            t = dt.datetime(int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4]), int(parts[5]))
        except ValueError:
            continue
        rec = decode(parts[6])
        if rec is None:
            continue
        rec["time"] = t.strftime("%Y-%m-%dT%H:%M")
        rows.append(rec)
    return rows


def parse_isd(text: str) -> list[dict]:
    """NOAA ISD full-format lines -> records like :func:`parse_ogimet`'s.

    ISD (``noaa-isd-pds`` on AWS) relays the same SYNOP reports. Where a line
    still carries the bulletin text (``REMSYN<len><report>``) it is decoded with
    :func:`decode`, so the 24 h extremes and precipitation groups are exactly
    what OGIMET would give. Lines that came in as BUFR (``REMSYN004BUFR``)
    carry only ISD's own coded groups, and for Chinese stations in 2025 their
    precipitation and extreme-temperature groups are unusable (24 h totals of
    0 on rainy days, 12 h maxima that do not move); only the instantaneous
    temperature and wind of the mandatory section are taken from those.

    ISD stopped updating on AWS in 2025 (succeeded by GHCNh), so this serves
    backfills and backtests of past seasons.
    """
    rows = []
    for line in text.splitlines():
        if len(line) < 105 or not line[4:10].isdigit():
            continue
        stamp = line[15:27]
        try:
            t = dt.datetime.strptime(stamp, "%Y%m%d%H%M")
        except ValueError:
            continue
        rec: dict = {"wmo": line[4:9]}
        k = line.find("REMSYN")
        if k >= 0 and "BUFR" not in line[k:k + 16] and line[k + 6:k + 9].isdigit():
            body = line[k + 9:k + 9 + int(line[k + 6:k + 9])]
            syn = decode(f"AAXX {t:%d%H}1 {body}")
            if syn:
                rec.update(syn)
        # mandatory section: air temperature (tenths ℃) and wind (tenths m/s), both QC-coded
        temp, tq = line[87:92], line[92]
        if "t" not in rec and temp not in ("+9999", "-9999") and tq in "01459":
            rec["t"] = int(temp) / 10.0
        wdir, wspd, wq = line[60:63], line[65:69], line[69]
        if wspd != "9999" and wq in "01459":
            rec["wind_speed"] = int(wspd) / 10.0
            rec["wind_dir"] = None if wdir == "999" or rec["wind_speed"] == 0 else float(int(wdir))
        rec["time"] = t.strftime("%Y-%m-%dT%H:%M")
        rows.append(rec)
    return rows


ISD = "https://noaa-isd-pds.s3.amazonaws.com/data"


def fetch_isd(station: Station, year: int, *, sess=None) -> list[dict]:
    """One station-year from NOAA ISD on AWS (see :func:`parse_isd`)."""
    import gzip

    sess = sess or session()
    blob = get(sess, f"{ISD}/{year}/{station.wmo}0-99999-{year}.gz", timeout=120, retries=3)
    return parse_isd(gzip.decompress(blob).decode("utf-8", "replace"))


# ------------------------------------------------------------------ fetching + store

def fetch(station: Station, begin: dt.datetime, end: dt.datetime, *, sess=None) -> list[dict]:
    """All reports from ``begin`` to ``end`` (UTC) for one station."""
    sess = sess or session()
    url = f"{OGIMET}?block={station.wmo}&begin={begin:%Y%m%d%H%M}&end={end:%Y%m%d%H%M}"
    return parse_ogimet(get(sess, url, timeout=90, retries=3).decode("utf-8", "replace"))


class ObsStore:
    """One JSON-lines file per station under ``root``; merges by report time."""

    def __init__(self, root: str | pathlib.Path):
        self.root = pathlib.Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, wmo: str) -> pathlib.Path:
        return self.root / f"{wmo}.jsonl"

    def load(self, wmo: str) -> dict[str, dict]:
        p = self.path(wmo)
        if not p.exists():
            return {}
        out = {}
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            out[r["time"]] = r
        return out

    def merge(self, wmo: str, rows: Iterable[dict]) -> int:
        cur = self.load(wmo)
        before = len(cur)
        for r in rows:
            cur[r["time"]] = r
        tmp = self.path(wmo).with_suffix(".part")
        tmp.write_text("".join(json.dumps(cur[k], ensure_ascii=False) + "\n" for k in sorted(cur)), encoding="utf-8")
        tmp.replace(self.path(wmo))
        return len(cur) - before

    def last_time(self, wmo: str) -> dt.datetime | None:
        cur = self.load(wmo)
        return dt.datetime.fromisoformat(max(cur)) if cur else None


def update(store: ObsStore, stations: Iterable[Station], *, days: int = 60, sess=None,
           now: dt.datetime | None = None, pause: float = POLITE_S) -> dict[str, int]:
    """Bring every station up to date (at most ``days`` back); returns new reports per station."""
    sess = sess or session()
    now = now or dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    added = {}
    for k, st in enumerate(stations):
        last = store.last_time(st.wmo)
        begin = max(now - dt.timedelta(days=days), (last - dt.timedelta(hours=24)) if last else now - dt.timedelta(days=days))
        if k:
            time.sleep(pause)
        try:
            rows = fetch(st, begin, now, sess=sess)
        except Exception:  # noqa: BLE001 — one station down must not stop the others
            added[st.wmo] = -1
            continue
        added[st.wmo] = store.merge(st.wmo, rows)
    return added


# ------------------------------------------------------------------ observed quantities

def _at(recs: dict[str, dict], t: dt.datetime, key: str):
    r = recs.get(t.strftime("%Y-%m-%dT%H:%M"))
    return None if r is None else r.get(key)


def daily_20_20(recs: dict[str, dict], date: dt.date) -> dict:
    """Observed values for the CMA day ``date`` (20:00 BJT on date-1 to 20:00 on date).

    ``tmax``/``tmin`` are the 24-hour extremes of the 12 UTC report, ``precip``
    its 24-hour total; ``precip_day`` / ``precip_night`` are the two 12-hour
    halves from the 6-hour totals (白天 = 08–20 BJT of ``date``,
    夜间 = 20 BJT of date-1 to 08 BJT of ``date``).
    """
    t12 = dt.datetime(date.year, date.month, date.day, 12)
    out = {"tmax": _at(recs, t12, "tx24"), "tmin": _at(recs, t12, "tn24"), "precip": _at(recs, t12, "r24")}

    def p6sum(ends):
        vals = [_at(recs, t, "p6") for t in ends]
        return None if any(v is None for v in vals) else float(sum(vals))

    out["precip_day"] = p6sum([t12 - dt.timedelta(hours=6), t12])
    out["precip_night"] = p6sum([t12 - dt.timedelta(hours=18), t12 - dt.timedelta(hours=12)])
    if out["precip"] is None and out["precip_day"] is not None and out["precip_night"] is not None:
        out["precip"] = out["precip_day"] + out["precip_night"]
    # wind: strongest reported 10-min mean in the day half and the night half
    for half, hrs in (("day", (3, 6, 9, 12)), ("night", (-9, -6, -3, 0))):
        speeds = [_at(recs, t12 - dt.timedelta(hours=12) + dt.timedelta(hours=h), "wind_speed") for h in hrs]
        speeds = [s for s in speeds if s is not None]
        out[f"wind_max_{half}"] = max(speeds) if len(speeds) >= 3 else None
    return out


def haversine_km(a_lat, a_lon, b_lat, b_lon) -> float:
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp, dl = p2 - p1, math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def as_townships(stations: Iterable[Station]):
    """Stations as :class:`wxgrid.points.Township` so the forecast pipeline can run on them."""
    from .points import Township
    return [Township(s.id, s.name, s.lat, s.lon, s.elevation_m) for s in stations]


def nanmean(xs) -> float:
    a = np.array([x for x in xs if x is not None], dtype=float)
    return float(a.mean()) if a.size else float("nan")
