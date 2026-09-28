"""Township point table: the thing that makes a county forecast "per-township".

A point table needs, per township: a stable id, a name, a representative
latitude/longitude, and a representative **elevation**. The elevation is not
decoration — it is the variable that separates one township from the next when
the model grid is 28 km wide.

Two ways in:

* :func:`load_csv` — your own table (recommended; weather bureaux already keep
  one, usually keyed by the 12-digit 统计用区划代码).
* :func:`from_geojson` — a township-boundary FeatureCollection; centroids are
  used as the representative point and the Copernicus DEM supplies the *areal
  mean* elevation, which is the statistically correct downscaling target.
"""

from __future__ import annotations

import csv
import pathlib
from dataclasses import asdict, dataclass

import numpy as np


@dataclass(frozen=True)
class Township:
    id: str
    name: str
    lat: float
    lon: float
    elevation_m: float
    parent: str = ""

    def __post_init__(self) -> None:
        if not -90.0 <= self.lat <= 90.0:
            raise ValueError(f"{self.name}: latitude {self.lat} out of range")
        if not -180.0 <= self.lon <= 180.0:
            raise ValueError(f"{self.name}: longitude {self.lon} out of range")


FIELDS = ("id", "name", "lat", "lon", "elevation_m", "parent")


def save_csv(points: list[Township], path: str | pathlib.Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for p in sorted(points, key=lambda p: p.id):
            w.writerow(asdict(p))


def load_csv(path: str | pathlib.Path) -> list[Township]:
    with open(path, newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise ValueError(f"{path}: empty township table")
    missing = [f for f in FIELDS[:5] if f not in rows[0]]
    if missing:
        raise ValueError(f"{path}: missing column(s) {missing}")
    return [
        Township(
            id=str(r["id"]).strip(),
            name=str(r["name"]).strip(),
            lat=float(r["lat"]),
            lon=float(r["lon"]),
            elevation_m=float(r["elevation_m"]),
            parent=str(r.get("parent") or "").strip(),
        )
        for r in rows
    ]


def from_geojson(path: str | pathlib.Path, *, dem=None, id_field: str | None = None,
                 name_field: str = "name") -> list[Township]:
    """Build a point table from township polygons.

    ``dem`` (a :class:`wxgrid.dem.CopernicusDEM`) is used for the areal-mean
    elevation inside each polygon; without it a plain centroid is produced and
    the caller must fill ``elevation_m`` later.
    """
    import json

    from shapely.geometry import shape

    collection = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    feats = collection.get("features", [])
    if not feats:
        raise ValueError(f"{path}: no features")

    out: list[Township] = []
    for i, f in enumerate(feats):
        props = f.get("properties") or {}
        geom = shape(f["geometry"])
        if geom.is_empty:
            continue
        rep = geom.representative_point()
        name = str(props.get(name_field) or props.get("NAME") or f"feature_{i}")
        tid = str(props.get(id_field) if id_field else props.get("adcode") or f"{i:04d}")
        elev = float(dem.area_mean(rep.y, rep.x, 0.0, polygon=geom)) if dem is not None else float("nan")
        out.append(Township(id=tid, name=name, lat=round(rep.y, 6), lon=round(rep.x, 6), elevation_m=elev))
    return out


def from_wikidata(county_label: str, dem=None, *, radius_deg: float = 0.01,
                  timeout: int = 90) -> list[Township]:
    """Build a point table from Wikidata for a county given by its Chinese label.

    Key-free and reliable, unlike the Overpass mirrors. Covers 镇/乡/街道, which
    is exactly the level a county forecast system reports at. Elevation comes
    from ``dem`` as the mean over ``radius_deg`` around the seat (~1.1 km by
    default), since Wikidata carries no boundary geometry.
    """
    import requests

    query = """
    SELECT ?item ?itemLabel ?coord ?typeLabel WHERE {
      ?county rdfs:label "%s"@zh ; wdt:P625 ?c .
      ?item wdt:P131 ?county ; wdt:P625 ?coord .
      OPTIONAL { ?item wdt:P31 ?type . }
      SERVICE wikibase:label { bd:serviceParam wikibase:language "zh,en". }
    }
    """ % county_label.replace('"', "")
    r = requests.get(
        "https://query.wikidata.org/sparql",
        params={"query": query, "format": "json"},
        headers={"User-Agent": "wxgrid/0.1", "Accept": "application/sparql-results+json"},
        timeout=timeout,
    )
    r.raise_for_status()

    admin = {"镇", "乡", "街道", "民族乡", "苏木", "民族苏木"}
    seen: dict[str, Township] = {}
    for b in r.json()["results"]["bindings"]:
        if str(b.get("typeLabel", {}).get("value", "")).strip() not in admin:
            continue
        name = b["itemLabel"]["value"].strip()
        if name in seen:
            continue
        lon, lat = (float(v) for v in b["coord"]["value"].removeprefix("Point(").rstrip(")").split())
        qid = b["item"]["value"].rsplit("/", 1)[-1]
        elev = float(dem.area_mean(lat, lon, radius_deg)) if dem is not None else float("nan")
        seen[name] = Township(id=qid, name=name, lat=round(lat, 6), lon=round(lon, 6),
                              elevation_m=elev, parent=county_label)
    if not seen:
        raise ValueError(f"Wikidata has no 镇/乡/街道 for {county_label!r}")
    return list(seen.values())


def elevation_spread(points: list[Township]) -> dict[str, float]:
    z = np.array([p.elevation_m for p in points], dtype=float)
    return {"n": float(z.size), "min_m": float(z.min()), "max_m": float(z.max()),
            "p90_minus_p10_m": float(np.percentile(z, 90) - np.percentile(z, 10))}
