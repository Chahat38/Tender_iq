"""Vercel Python serverless entrypoint for the TenderScout API.

``vercel dev`` / ``vercel deploy`` build a Python function from this
file; the Vercel Python runtime serves the ASGI ``app`` object. The
collector package is imported from the sibling ``collector/`` directory,
and ``DATABASE_URL`` (set as a Vercel environment variable) decides the
real backend: absent -> local SQLite, present -> Supabase/Postgres via
:mod:`collector.storage_postgres` (which also powers ``/search``).
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_COLLECTOR = os.path.join(_ROOT, "collector")
if _COLLECTOR not in sys.path:
    sys.path.insert(0, _COLLECTOR)

# Importing collector/api builds the ASGI app (module-level `app`).
from api import app as app  # noqa: E402,F401  (Vercel imports this name)