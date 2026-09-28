"""Remote forecast sources. Each module exposes ``latest_run`` / ``available_steps`` / ``fetch``."""

from __future__ import annotations

from . import ecmwf, gfs

REGISTRY = {
    "ecmwf": ecmwf,
    "gfs": gfs,
}

__all__ = ["REGISTRY", "ecmwf", "gfs"]
