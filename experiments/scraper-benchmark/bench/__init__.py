"""Benchmark package (throwaway scaffolding, not SEAL production code)."""
from __future__ import annotations

import sys
from pathlib import Path

# allow `python scrapy_test.py` from the benchmark directory
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))