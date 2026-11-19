#!/usr/bin/env python3
"""Convenience runner for scripts/download_dataset.py from within the data folder."""
import sys
from pathlib import Path

script_path = Path(__file__).resolve().parent.parent / "scripts" / "download_dataset.py"
if __name__ == "__main__":
    import runpy
    sys.argv[0] = str(script_path)
    runpy.run_path(str(script_path), run_name="__main__")
