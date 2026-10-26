#!/usr/bin/env python3
"""Run the native DeepSeek URL adapter from an isolated attempt directory."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from defipi.execution.api.deepseek_url import main

if __name__ == "__main__":
    raise SystemExit(main())
