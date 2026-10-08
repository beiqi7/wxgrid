"""Command line entry point: ``python -m wxgrid <command>``."""

from __future__ import annotations

import argparse
import sys

from . import blend as blending
from . import calibrate as calibration
from . import daily as daily_mod
from . import dem as dem_mod
from . import downscale, pipeline, points as points_mod, report, store
from .sources import REGISTRY
from .sources._fetch import session
from .runs import Run


def _parse_weights(text: str | None) -> dict[str, float] | None:
    if not text:
        return None
    out: dict[str, float] = {}
    for part in text.split(","):
        key, _, value = part.partition("=")
        if not value:
            raise SystemExit(f"--weights expects name=value pairs, got {part!r}")
        out["ecmwf-ifs-0p25" if key.strip() == "ecmwf" else "gfs-0p25" if key.strip() == "gfs" else key.strip()] = float(value)
    return out


def _load_points(args) -> list[points_mod.Township]:
    if args.townships:
        pts = points_mod.load_csv(args.townships)
        if any(p.elevation_m != p.elevation_m for p in pts):  # NaN
            raise SystemExit(f"{args.townships}: elevation_m has NaN — fill it or use `townships` first")
        return pts
    raise SystemExit("--townships is required (see `python -m wxgrid townships --help`)")


def cmd_runs(args) -> int:
    sess = session()
    for name in args.sources.split(","):
        mod = REGISTRY[name]
        run = mod.latest_run(sess)
        steps = mod.available_steps(sess, run)
        print(f"{name:6s} run={run}  init={run.init_time:%Y-%m-%dT%H:%MZ}  "
              f"steps={len(steps)}  max={max(steps)}h  cadence={_cadence(steps)}h")
    return 0


def _cadence(steps: list[int]) -> int:
    diffs = [b - a for a, b in zip(steps, steps[1:]) if b > a]
    return min(diffs) if diffs else 0


def cmd_townships(args) -> int:
    if not args.geojson and not args.wikidata:
        raise SystemExit("give either --geojson (township polygons) or --wikidata (county label)")
    dem = dem_mod.CopernicusDEM(cache_dir=args.dem_cache)
    if args.geojson:
        pts = points_mod.from_geojson(args.geojson, dem=dem, id_field=args.id_field, name_field=args.name_field)
    else:
        pts = points_mod.from_wikidata(args.wikidata, dem=dem, radius_deg=args.radius_deg)
    points_mod.save_csv(pts, args.out)
    spread = points_mod.elevation_spread(pts)
    print(f"wrote {args.out}: {len(pts)} townships, "
          f"elevation {spread['min_m']:.0f}-{spread['max_m']:.0f} m "
          f"(p90-p10 = {spread['p90_minus_p10_m']:.0f} m)")
    return 0


def _run_forecast(args):
    pts = _load_points(args)
    hours = args.days * 24 if getattr(args, "days", None) else args.hours
    steps = pipeline.step_grid(hours, args.every)
    sources = tuple(args.sources.split(","))
    per_source = pipeline.forecast(pts, steps=steps, sources=sources, pad=args.pad,
                                   weights=_parse_weights(args.weights), cfg=downscale.DownscaleConfig())
    if getattr(args, "calibration", None):
        cal = calibration.Calibration.from_json(args.calibration)
        per_source = {k: calibration.apply(ds, cal) for k, ds in per_source.items()}
    return pts, per_source, hours


def cmd_daily(args) -> int:
    pts, per_source, hours = _run_forecast(args)
    member = args.member if args.member in per_source else next(reversed(per_source))
    daily = daily_mod.to_daily(per_source[member], tz_hours=args.tz)
    print(f"{args.county or ''} 未来{args.days}天  成员={member}  "
          f"{len(pts)}个乡镇  {hours}h")
    print(report.full_report(daily, county=args.county, days=args.days))
    if args.out:
        daily.to_netcdf(args.out)
        print(f"\nwrote {args.out}")
    return 0


def cmd_bulletin(args) -> int:
    """County-seat + per-township bulletin in 白天/夜间 periods (no probabilities)."""
    import json

    from . import product

    pts = _load_points(args)
    sources = tuple(args.sources.split(","))
    run = Run.from_stamp(args.run) if args.run else None
    doc = product.compute(pts, county=args.county, seat=args.seat, days=args.days, every=args.every,
                          sources=sources, member=args.member, weights=_parse_weights(args.weights),
                          tz=args.tz, pad=args.pad, want_pop=not args.no_pop, workers=args.workers,
                          min_age_hours=args.min_age_hours, run=run, engine=args.engine,
                          data_dir=args.data_dir)
    print(doc["text"])
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False)
        print(f"\nwrote {args.out}")
    return 0


def cmd_fetch(args) -> int:
    pts = _load_points(args)
    hours = args.days * 24 if args.days else args.hours
    print(f"{len(pts)} townships @ {hours}h every {args.every}h from {args.sources.split(',')}")
    per_source = pipeline.forecast(pts, steps=pipeline.step_grid(hours, args.every),
                                   sources=tuple(args.sources.split(",")), pad=args.pad,
                                   weights=_parse_weights(args.weights),
                                   cfg=downscale.DownscaleConfig())
    if args.calibration:
        cal = calibration.Calibration.from_json(args.calibration)
        per_source = {k: calibration.apply(ds, cal) for k, ds in per_source.items()}

    for name, ds in per_source.items():
        t = ds["t2m"]
        p = ds["precip"]
        print(f"  {name:18s} init={ds.attrs['init_time']} steps={ds.sizes['step']} "
              f"t2m[{float(t.min()):.1f},{float(t.max()):.1f}]C  "
              f"spread={float(ds['t2m'].isel(step=-1).max() - ds['t2m'].isel(step=-1).min()):.1f}K  "
              f"precip_max={float(p.max()):.1f}mm")

    if args.out:
        frames = [store.to_frame(ds, name) for name, ds in per_source.items()]
        n = store.write_sqlite(frames, args.out)
        print(f"wrote {n} rows -> {args.out}")
    if args.netcdf:
        store.write_netcdf(per_source, args.netcdf)
        print(f"wrote {args.netcdf}")
    if args.weights_from_station_rmse:
        score = {}
        for part in args.weights_from_station_rmse.split(","):
            k, _, v = part.partition("=")
            score["ecmwf-ifs-0p25" if k == "ecmwf" else "gfs-0p25" if k == "gfs" else k] = float(v)
        print("inverse-variance weights:", {k: round(v, 3) for k, v in blending.weights_from_skill(score).items()})
    return 0


def cmd_calibrate(args) -> int:
    import pandas as pd

    preds = []
    for path in args.forecast.split(","):
        import xarray as xr

        ds = xr.open_dataset(path) if path.endswith((".nc", ".netcdf")) else None
        if ds is None:
            raise SystemExit(f"only NetCDF forecast inputs are supported, got {path}")
        for member in ds["member"].values:
            preds.append(store.to_frame(ds.sel(member=member), str(member)))
    obs = pd.read_csv(args.obs)
    merged = pd.concat(preds, ignore_index=True)
    merged["valid_time"] = pd.to_datetime(merged["valid_time"])
    best = merged[merged["source"] == args.source] if args.source else merged
    cal = calibration.fit(best.set_index(["point", "valid_time"]).to_xarray(), obs, min_obs=args.min_obs)
    cal.to_json(args.out)
    print(f"wrote {args.out}: {len(cal.bias_t2m)} temperature, "
          f"{len(cal.wind_factor)} wind, {len(cal.precip_factor)} precipitation entries")
    return 0


def cmd_verify(args) -> int:
    """Refresh the station archive, refit the calibration, print the scores."""
    import json
    import pathlib

    from . import postproc
    if args.engine == "native":
        from . import consensus
        data = pathlib.Path(args.data_dir)
        cal = consensus.refresh(data / "native", archive_root=data / "archive", obs_root=data / "verify" / "obs",
                                max_age_days=0 if args.force else 1)
        scores = consensus.load_scores(data / "native")
    else:
        cal = postproc.refresh(args.dir, max_age_days=0 if args.force else 1)
        scores = postproc.load_scores(args.dir)
    if not cal or not scores or scores.get("error"):
        print("no calibration yet:", (scores or {}).get("error", "archive empty"))
        return 1
    print(f"calibration {cal['window'][0]} .. {cal['window'][1]}, {cal['n_pairs']} pairs"
          + (f", alpha {cal['alpha']}" if "alpha" in cal else ""))
    print(f"scores {scores['window'][0]} .. {scores['window'][1]} ({scores['days']} days, stations {', '.join(scores['stations'])})")
    print(f"{'':30s} " + "  ".join(f"{'第' + str(l) + '天':^27s}" for l in (1, 3, 5)))
    for key in scores.get("order") or scores["configs"]:
        c = scores["configs"][key]
        cells = []
        for lead in ("1", "3", "5"):
            s = c["scores"].get(lead) or c["scores"].get(int(lead))
            cells.append(f"Tx {s['tmax']['mae']:4.2f}/{s['tmax']['acc2']:3.0f}% Tn {s['tmin']['mae']:4.2f} 晴雨{s['rain']['pc']:3.0f}%")
        print(f"{c['label'][:28]:30s} " + "  ".join(cells))
    if args.json:
        print(json.dumps(scores, ensure_ascii=False, indent=1, default=float))
    return 0


def cmd_backfill(args) -> int:
    """Seed the native engine's run archive from the producers' historical open data."""
    import datetime as dt
    import pathlib

    from . import archive, consensus
    from . import obs as obs_mod
    from .gribbox import Box

    pts = _load_points(args)
    data = pathlib.Path(args.data_dir)
    end = dt.date.fromisoformat(args.end) if args.end else dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    start = end - dt.timedelta(days=args.days - 1)
    box = Box.around([*pts, *obs_mod.NEAR_YANSHAN], pad=0.75)
    models = args.models.split(",")
    print(f"backfill {start} .. {end}, cycles {args.cycles}, {models} -> {data / 'archive'}")
    done = archive.backfill(data / "archive", models=models, start=start, end=end, box=box,
                            cycles=tuple(int(c) for c in args.cycles.split(",")), last_step=144,
                            workers=args.workers)
    print("stored:", done)
    if not args.no_fit:
        cal = consensus.refresh(data / "native", archive_root=data / "archive", obs_root=data / "verify" / "obs",
                                max_age_days=0)
        print("calibration:", "none yet" if not cal else f"{cal['window']} ({cal['n_pairs']} pairs)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="wxgrid", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("runs", help="show the newest published cycle per source")
    r.add_argument("--sources", default="ecmwf,gfs")
    r.set_defaults(func=cmd_runs)

    t = sub.add_parser("townships", help="build a township point table")
    t.add_argument("--geojson", default=None, help="FeatureCollection of township polygons")
    t.add_argument("--wikidata", default=None, help='county label, e.g. "缙云县" (key-free)')
    t.add_argument("--out", required=True)
    t.add_argument("--id-field", default=None)
    t.add_argument("--name-field", default="name")
    t.add_argument("--radius-deg", type=float, default=0.01, help="DEM averaging radius for --wikidata")
    t.add_argument("--dem-cache", default="./dem-cache")
    t.set_defaults(func=cmd_townships)

    d = sub.add_parser("daily", help="print the N-day township bulletin")
    d.add_argument("--townships", required=True)
    d.add_argument("--county", default="")
    d.add_argument("--days", type=int, default=5)
    d.add_argument("--every", type=int, default=3)
    d.add_argument("--tz", type=float, default=daily_mod.TZ_CHINA)
    d.add_argument("--member", default="blend", help="blend | ecmwf-ifs-0p25 | gfs-0p25")
    d.add_argument("--sources", default="ecmwf,gfs")
    d.add_argument("--pad", type=float, default=0.75)
    d.add_argument("--weights", default=None)
    d.add_argument("--calibration", default=None)
    d.add_argument("--out", default=None, help="optional NetCDF of the daily aggregates")
    d.set_defaults(func=cmd_daily)

    b = sub.add_parser("bulletin", help="county-seat + per-township bulletin in 白天/夜间 periods")
    b.add_argument("--townships", required=True)
    b.add_argument("--county", default="")
    b.add_argument("--seat", required=True, help="name of the county-seat township")
    b.add_argument("--days", type=int, default=5)
    b.add_argument("--every", type=int, default=3)
    b.add_argument("--tz", type=float, default=daily_mod.TZ_CHINA)
    b.add_argument("--member", default="blend")
    b.add_argument("--sources", default="ecmwf,gfs")
    b.add_argument("--no-pop", action="store_true",
                   help="skip the ensemble download (probabilities only appear in the JSON)")
    b.add_argument("--workers", type=int, default=8)
    b.add_argument("--pad", type=float, default=0.75)
    b.add_argument("--weights", default=None)
    b.add_argument("--run", default=None)
    b.add_argument("--min-age-hours", type=float, default=None,
                   help="ignore cycles younger than this (default 2, i.e. use the settled cycle)")
    b.add_argument("--engine", choices=("multimodel", "native", "grib"), default="multimodel")
    b.add_argument("--data-dir", default="/var/lib/wxgrid", help="run archive + calibration of the native engine")
    b.add_argument("--out", default=None, help="also write the product JSON here")
    b.set_defaults(func=cmd_bulletin)

    f = sub.add_parser("fetch", help="download, downscale and blend a county forecast")
    f.add_argument("--townships", required=True, help="township CSV (id,name,lat,lon,elevation_m)")
    f.add_argument("--hours", type=int, default=72)
    f.add_argument("--days", type=int, default=None, help="shorthand: hours = days * 24")
    f.add_argument("--every", type=int, default=3, help="step cadence in hours (ECMWF IFS is 3-hourly)")
    f.add_argument("--sources", default="ecmwf,gfs")
    f.add_argument("--pad", type=float, default=0.75, help="model-grid margin around the county, degrees")
    f.add_argument("--weights", default=None, help='e.g. "ecmwf=0.6,gfs=0.4"')
    f.add_argument("--calibration", default=None, help="Calibration JSON from the calibrate command")
    f.add_argument("--out", default=None, help="SQLite output path")
    f.add_argument("--netcdf", default=None)
    f.add_argument("--weights-from-station-rmse", default=None,
                   help='e.g. "ecmwf=1.4,gfs=1.9" — prints inverse-variance weights')
    f.set_defaults(func=cmd_fetch)

    c = sub.add_parser("calibrate", help="fit per-township bias corrections from station observations")
    c.add_argument("--forecast", required=True, help="comma-separated NetCDF files with a member dimension")
    c.add_argument("--obs", required=True, help="CSV with point,valid_time[,t2m,wind_speed,precip]")
    c.add_argument("--source", default=None, help="restrict to one member source")
    c.add_argument("--min-obs", type=int, default=20)
    c.add_argument("--out", required=True)
    c.set_defaults(func=cmd_calibrate)

    v = sub.add_parser("verify", help="update station observations, refit calibration, print skill scores")
    v.add_argument("--dir", default="/var/lib/wxgrid/verify")
    v.add_argument("--force", action="store_true", help="refit even if the calibration is less than a day old")
    v.add_argument("--json", action="store_true")
    v.add_argument("--engine", choices=("multimodel", "native"), default="multimodel")
    v.add_argument("--data-dir", default="/var/lib/wxgrid", help="native engine: archive/, native/, verify/obs/ under it")
    v.set_defaults(func=cmd_verify)

    bf = sub.add_parser("backfill", help="seed the native engine's run archive from historical open data")
    bf.add_argument("--townships", required=True)
    bf.add_argument("--data-dir", default="/var/lib/wxgrid")
    bf.add_argument("--days", type=int, default=60, help="how many days back (default 60, the training window)")
    bf.add_argument("--end", default=None, help="last day, YYYY-MM-DD (default yesterday, UTC)")
    bf.add_argument("--cycles", default="0,12")
    bf.add_argument("--models", default="ifs,aifs,gfs,gefs")
    bf.add_argument("--workers", type=int, default=12)
    bf.add_argument("--no-fit", action="store_true", help="skip the observation update and fit at the end")
    bf.set_defaults(func=cmd_backfill)

    pub = sub.add_parser("publish", add_help=False,
                         help="compute a product and store it under --data-dir (see `publish --help`)")
    pub.set_defaults(func=lambda a: _delegate("publish", a))

    srv = sub.add_parser("serve", add_help=False,
                         help="serve the read-only API + web UI (see `serve --help`)")
    srv.set_defaults(func=lambda a: _delegate("serve", a))
    return p


def _delegate(name: str, args) -> int:
    """Hand the remaining argv to the submodule's own parser."""
    rest = list(getattr(args, "_rest", []))
    if name == "publish":
        from . import publish as publish_mod
        return publish_mod.main(rest)
    from .web import app as web_app
    return web_app.main(rest)


def main(argv: list[str] | None = None) -> int:
    args, rest = build_parser().parse_known_args(argv)
    args._rest = rest
    if rest and args.command not in ("publish", "serve"):
        raise SystemExit(f"unrecognised arguments: {' '.join(rest)}")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
