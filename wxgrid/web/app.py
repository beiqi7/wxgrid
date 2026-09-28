"""Read-only JSON API + static file server for published forecast products.

Routes::

    GET /                     the web UI (static/index.html)
    GET /api/health           {"status","runs","latest"}
    GET /api/latest           newest product (full JSON)
    GET /api/summary          newest product's meta + conclusions only (small)
    GET /api/runs             index of stored runs, newest first
    GET /api/runs/<file>      one stored product by its index `file` name
    GET /api/townships        static roster from the newest product
    GET /api/hourly           newest hourly series, all townships (columnar)
    GET /api/hourly/<id|name> one township's hourly rows; ?run=<file> ?hours=N

Everything is served from ``--data-dir``; the server does no computation.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import pathlib
import socketserver
import sys
from typing import Any
from urllib.parse import parse_qs, unquote

DEFAULT_DATA_DIR = os.environ.get("WXGRID_DATA_DIR", "/var/lib/wxgrid")
STATIC_DIR = pathlib.Path(__file__).parent / "static"


class _Handler(http.server.BaseHTTPRequestHandler):
    data_dir: pathlib.Path = pathlib.Path(DEFAULT_DATA_DIR)
    server_version = "wxgrid/0.1"

    # -- helpers ----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, *, cache: int = 0) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")  # read-only public data
        if cache:
            self.send_header("Cache-Control", f"public, max-age={cache}")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200, *, cache: int = 0) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", cache=cache)

    def _error(self, code: int, msg: str) -> None:
        self._json({"error": msg}, code=code)

    def _read_json(self, name: str) -> Any | None:
        path = self.data_dir / name
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None

    # -- routing ----------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        # Percent-decode first: stored run files carry Chinese county names.
        raw_path, _, raw_query = self.path.partition("?")
        path = unquote(raw_path).rstrip("/") or "/"
        query = parse_qs(raw_query)
        try:
            if path == "/" or path == "/index.html":
                return self._static("index.html")
            if path.startswith("/static/"):
                return self._static(path[len("/static/"):])
            if path == "/api/health":
                return self._health()
            if path == "/api/latest":
                return self._latest()
            if path == "/api/summary":
                return self._summary()
            if path == "/api/runs":
                return self._runs()
            if path.startswith("/api/runs/"):
                return self._run_file(path[len("/api/runs/"):])
            if path == "/api/townships":
                return self._townships()
            if path == "/api/hourly":
                return self._hourly(None, query)
            if path.startswith("/api/hourly/"):
                return self._hourly(path[len("/api/hourly/"):], query)
            self._error(404, f"no route {path}")
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._error(500, f"{type(exc).__name__}: {exc}")

    # -- endpoints --------------------------------------------------------
    def _health(self) -> None:
        idx = self._read_json("index.json") or []
        latest = self._read_json("latest.json") or {}
        self._json({"status": "ok", "runs": len(idx),
                    "latest": (latest.get("meta") or {}).get("run"),
                    "hourly": bool(idx and idx[0].get("hourly"))})

    def _latest(self) -> None:
        obj = self._read_json("latest.json")
        self._json(obj, cache=300) if obj is not None else self._error(404, "no product published yet")

    def _summary(self) -> None:
        obj = self._read_json("latest.json")
        if obj is None:
            return self._error(404, "no product published yet")
        self._json({"meta": obj.get("meta"), "conclusions": obj.get("conclusions"),
                    "days": [{"date": d["date"], "weekday": d["weekday"], "county": d["county"]}
                             for d in obj.get("days", [])]}, cache=300)

    def _runs(self) -> None:
        self._json(self._read_json("index.json") or [], cache=60)

    def _run_file(self, name: str) -> None:
        # Strict: index filenames only, no path separators — blocks traversal.
        if not _safe_run_name(name):
            return self._error(400, "bad run id")
        obj = self._read_json(f"runs/{name}")
        self._json(obj, cache=3600) if obj is not None else self._error(404, f"no run {name}")

    def _townships(self) -> None:
        obj = self._read_json("latest.json")
        if obj is None:
            return self._error(404, "no product published yet")
        self._json({"townships": obj.get("townships", []),
                    "county": (obj.get("meta") or {}).get("county")}, cache=300)

    def _hourly(self, key: str | None, query: dict[str, list[str]]) -> None:
        """``/api/hourly`` (all townships, columnar) or ``/api/hourly/<id|name>`` (rows).

        ``?run=<file>`` picks a stored cycle by its index ``file``; default newest.
        ``?hours=N`` trims the one-township view to the first N hours.
        """
        run = (query.get("run") or [None])[0]
        if run is not None:
            if not _safe_run_name(run):
                return self._error(400, "bad run id")
            name = run
        else:
            idx = self._read_json("index.json") or []
            if not idx:
                return self._error(404, "no product published yet")
            name = idx[0]["file"]
        doc = self._read_json(f"hourly/{name}")
        if doc is None:
            return self._error(404, f"no hourly series for {name}")
        cache = 3600 if run else 300
        if key is None:
            return self._json(doc, cache=cache)
        view = township_hours(doc, key)
        if view is None:
            return self._error(404, f"no township {key!r} (use an id or a name from /api/townships)")
        n = (query.get("hours") or [None])[0]
        if n is not None:
            if not n.isdigit() or int(n) < 1:
                return self._error(400, "hours must be a positive integer")
            view["hours"] = view["hours"][: int(n)]
        self._json(view, cache=cache)

    def _static(self, rel: str) -> None:
        rel = rel.lstrip("/")
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            return self._error(404, "not found")
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml",
                 ".json": "application/json"}.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        # Content ETag + must-revalidate: a redeploy takes effect on the next
        # request instead of serving a stale bundle for the rest of a max-age.
        etag = '"%s"' % hashlib.sha1(body).hexdigest()[:16]
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # quieter default logging
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _safe_run_name(name: str) -> bool:
    """A bare stored-run file name: no separators, no reserved pointer files."""
    return ("/" not in name and "\\" not in name and name.endswith(".json")
            and not name.startswith(".") and name not in ("index.json", "latest.json"))


#: Per-hour fields copied from the columnar hourly file into row form.
_HOUR_FIELDS = ("weather", "temp", "precip", "snow", "cloud", "wind_speed", "wind_dir",
                "wind_name", "wind_force", "gust", "pop")


def township_hours(doc: dict, key: str) -> dict | None:
    """One township's hourly series as rows, looked up by id or by name.

    Pure stdlib on purpose: the web process never imports numpy/xarray.
    """
    points = doc.get("points") or {}
    pid = key if key in points else next((k for k, v in points.items() if v.get("name") == key), None)
    if pid is None:
        return None
    p = points[pid]
    times, leads = doc.get("times") or [], doc.get("lead_h") or []
    cols = {f: p.get(f) for f in _HOUR_FIELDS if isinstance(p.get(f), list)}
    rows = []
    for i, t in enumerate(times):
        row: dict = {"time": t, "lead_h": leads[i] if i < len(leads) else None}
        for f, col in cols.items():
            row[f] = col[i] if i < len(col) else None
        rows.append(row)
    return {"meta": doc.get("meta"),
            "township": {"id": pid, "name": p.get("name"), "lat": p.get("lat"),
                         "lon": p.get("lon"), "elevation": p.get("elevation")},
            "hours": rows}


def build_server(host: str, port: int, data_dir: str) -> _Server:
    handler = type("Handler", (_Handler,), {"data_dir": pathlib.Path(data_dir)})
    return _Server((host, port), handler)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="wxgrid-web", description=__doc__)
    p.add_argument("--host", default=os.environ.get("WXGRID_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("WXGRID_PORT", "8790")))
    p.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    args = p.parse_args(argv)
    srv = build_server(args.host, args.port, args.data_dir)
    print(f"wxgrid web on http://{args.host}:{args.port}  (data-dir={args.data_dir})", file=sys.stderr)
    if args.host in ("0.0.0.0", "::"):
        print("note: bound to all interfaces and UNAUTHENTICATED — put a reverse proxy in front "
              "if this is internet-facing.", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
