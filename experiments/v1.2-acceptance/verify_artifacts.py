"""Verify frozen synthetic evidence hashes and truthful recorded assertion outcomes."""

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "v1.1-runtime-acceptance/verify_artifacts.py"),
        run_name="__main__",
    )
