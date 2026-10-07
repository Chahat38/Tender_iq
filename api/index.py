"""Vercel Python serverless entrypoint for the TenderScout API.

``vercel dev`` / ``vercel deploy`` build a Python function from this file;
the Vercel Python runtime serves the ASGI ``app`` object.

Database selection happens here before the app is built:

* If ``DATABASE_URL`` is set, the API serves from Supabase/Postgres via
  :mod:`collector.storage_postgres` (``/search`` uses the ILIKE fallback).
* Otherwise it falls back to a bundled SQLite snapshot
  (``collector/demo/db.tenderscout`` - the 4 400+ row collection demo). The
  serverless filesystem is read-only, so the snapshot is copied to the
  writable temp dir and ``TENDERSCOUT_DB_PATH`` points at the copy.
"""

import os
import shutil
import sys
import tempfile

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_COLLECTOR = os.path.join(_ROOT, "collector")
if _COLLECTOR not in sys.path:
    sys.path.insert(0, _COLLECTOR)

_DEMO_DB = os.path.join(_COLLECTOR, "demo", "db.tenderscout")
if "DATABASE_URL" not in os.environ and os.path.exists(_DEMO_DB):
    _target = os.path.join(tempfile.gettempdir(), "tenderscout.db")
    shutil.copy2(_DEMO_DB, _target)
    os.environ["TENDERSCOUT_DB_PATH"] = _target

# Importing collector/api builds the ASGI app (module-level `app`).
from api import app as _app  # noqa: E402


class _ApiPrefixStrip:
    """Normalize the path FastAPI sees.

    Vercel routes `api/index.py` behind a rewrite; depending on ETL the
    function may receive the request with a leading `/api` (e.g. from a
    `/api`-based proxy) or with the original public path. Strip one
    leading `/api` segment so the non-prefixed route table always
    matches, then hand off to the app.
    """

    def __init__(self, application):
        self.application = application

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path", "").startswith("/api"):
            scope = dict(scope)
            stripped = scope["path"][4:] or "/"
            scope["path"] = stripped
            scope["raw_path"] = stripped.encode("ascii", "ignore")
        await self.application(scope, receive, send)


app = _ApiPrefixStrip(_app)