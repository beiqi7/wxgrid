"""Read-only web layer: a JSON API over the published products, plus a static UI.

Stdlib only (``http.server``) — no framework, no extra dependency. The server
never computes a forecast; it only serves what :mod:`wxgrid.publish` wrote, so it
is cheap and safe to expose. It is unauthenticated by design (the data is public
weather); bind it to localhost and reverse-proxy if you need auth or TLS.
"""
from .app import build_server, main

__all__ = ["build_server", "main"]
