"""Shared HTTP + GRIB plumbing for the remote forecast sources."""

from __future__ import annotations

import io
import os
import tempfile
import threading
import time
import warnings
from typing import Iterable
from urllib.parse import urlparse

import cfgrib
import requests
import xarray as xr

USER_AGENT = "wxgrid/0.1 (+https://example.invalid/wxgrid)"
_TIMEOUT = 60
_RETRIES = 8
_BACKOFF_CAP = 45.0

#: Sustained requests per second per host. S3 answers a shared-prefix burst with
#: 503 SlowDown and no hint about how long to wait, so the fix is not to burst.
#: The ECMWF mirror takes short bursts of 35 req/s, but half an hour at 20 req/s
#: drew bucket-wide SlowDown (2026-10); 8 is sustainable.
_HOST_RATE = {
    "ecmwf-forecasts.s3.eu-central-1.amazonaws.com": 8.0,
    "data.ecmwf.int": 4.0,
    "noaa-gfs-bdp-pds.s3.amazonaws.com": 25.0,
    "noaa-gefs-pds.s3.amazonaws.com": 50.0,   # ~900 small reads per cycle (21 members x 2 per step)
}
_DEFAULT_RATE = 20.0
_buckets: dict[str, list[float]] = {}
_bucket_lock = threading.Lock()


def _throttle(url: str) -> None:
    """Token bucket: block until this host is allowed another request."""
    host = urlparse(url).netloc
    rate = _HOST_RATE.get(host, _DEFAULT_RATE)
    with _bucket_lock:
        state = _buckets.setdefault(host, [rate, time.monotonic()])
    while True:
        with _bucket_lock:
            now = time.monotonic()
            tokens = min(rate, state[0] + (now - state[1]) * rate)
            state[0], state[1] = tokens, now
            if tokens >= 1.0:
                state[0] = tokens - 1.0
                return
            wait = (1.0 - tokens) / rate
        time.sleep(min(wait, 0.5))


def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def get(sess: requests.Session, url: str, *, byte_range: tuple[int, int] | None = None,
        timeout: int = _TIMEOUT, retries: int | None = None) -> bytes:
    """GET with retries; optional inclusive byte range. Returns the body bytes.

    ``retries=1`` makes the call single-shot, for callers that do their own
    failover (see the ECMWF mirror rotation).
    """
    headers = {}
    if byte_range is not None:
        lo, hi = byte_range
        headers["Range"] = f"bytes={lo}-{hi}"
    last: Exception | None = None
    for attempt in range(retries or _RETRIES):
        try:
            _throttle(url)
            r = sess.get(url, headers=headers, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            if byte_range is not None:
                want = byte_range[1] - byte_range[0] + 1
                if len(r.content) != want:
                    raise requests.HTTPError(f"short range read: {len(r.content)} != {want}", response=r)
            return r.content
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status not in (429, 500, 502, 503, 504):
                raise  # 404/403 will not fix itself
            last = exc
        except requests.RequestException as exc:
            last = exc
        if attempt == (retries or _RETRIES) - 1:
            break
        # data.ecmwf.int rejects roughly half of a burst with 429 and sends no
        # Retry-After; S3 sends 503 SlowDown. Back off long enough to outlast it.
        import random

        retry_after = None
        if isinstance(last, requests.HTTPError) and last.response is not None:
            retry_after = last.response.headers.get("Retry-After")
        delay = float(retry_after) if (retry_after or "").isdigit() else \
            min(_BACKOFF_CAP, 1.0 * (2 ** attempt)) * (0.5 + random.random())
        time.sleep(delay)
    raise RuntimeError(f"GET failed after {retries or _RETRIES} attempts: {url}") from last


def head_ok(sess: requests.Session, url: str, timeout: int = 20) -> bool:
    try:
        return sess.head(url, timeout=timeout, allow_redirects=True).status_code == 200
    except requests.RequestException:
        return False


def decode_grib(blob: bytes) -> xr.Dataset:
    """Decode concatenated GRIB2 messages into one Dataset.

    ``cfgrib`` splits messages into hypercubes (one per height above ground),
    so several datasets come back and have to be merged on the shared grid.
    """
    fd, path = tempfile.mkstemp(suffix=".grib2")
    try:
        os.write(fd, blob)
        os.close(fd)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)
            parts = cfgrib.open_datasets(path, backend_kwargs={"indexpath": ""})
        if not parts:
            raise ValueError("no GRIB messages decoded")
        # Scalar coords (step, time, heightAboveGround, surface, ...) differ between
        # hypercube groups and would block the merge; the callers rebuild them.
        cleaned = [d.drop_vars([c for c in d.coords if c not in d.dims], errors="ignore") for d in parts]
        merged = xr.merge(cleaned, compat="override", join="exact")
        merged.attrs.pop("history", None)
        # cfgrib is lazy and keeps the temp path; materialise before we unlink it.
        return merged.load()
    finally:
        if os.path.exists(path):
            os.unlink(path)


def _cache_dir() -> "os.PathLike | None":
    raw = os.environ.get("WXGRID_CACHE")
    if not raw:
        return None
    path = os.path.expanduser(raw)
    os.makedirs(path, exist_ok=True)
    return path


def concat_messages(sess: requests.Session, url: str, spans: Iterable[tuple[int, int]]) -> bytes:
    """Fetch several byte ranges and concatenate them.

    Set ``WXGRID_CACHE`` to a directory to keep the result on disk — an ingest
    job that re-runs a cycle (retry, re-blend, re-downscale) then costs no
    bandwidth.
    """
    spans = list(spans)
    cache = _cache_dir()
    key = None
    if cache is not None:
        import hashlib

        key = hashlib.sha1((url + repr(spans)).encode()).hexdigest()
        path = os.path.join(cache, f"{key}.grib2")
        if os.path.exists(path):
            with open(path, "rb") as fh:
                return fh.read()
    else:
        path = None

    buf = io.BytesIO()
    for lo, length in spans:
        buf.write(get(sess, url, byte_range=(lo, lo + length - 1)))
    blob = buf.getvalue()
    if path:
        tmp = f"{path}.{os.getpid()}.part"
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, path)
    return blob
