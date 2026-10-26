#!/usr/bin/env python3
"""Run Pi in the shared agent container from any attempt working directory."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from defipi.execution.agents.pi import main

if __name__ == "__main__":
    raise SystemExit(main())
