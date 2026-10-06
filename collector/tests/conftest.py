"""Pytest bootstrap for collector tests.

Puts the ``collector`` directory on ``sys.path`` so tests can import the
collector modules directly. All tests run offline on saved fixtures and
in-memory data; no test ever touches the live network.
"""

import sys
from pathlib import Path

COLLECTOR_DIR = Path(__file__).resolve().parents[1]
if str(COLLECTOR_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR_DIR))
