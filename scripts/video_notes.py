#!/usr/bin/env python3
"""Run the bundled package without installing it into a Python environment."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from video_notes.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
