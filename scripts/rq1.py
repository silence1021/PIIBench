"""RQ1: publisher defenses with attack processing OFF."""
from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.reproduce import main

if __name__ == "__main__":
    raise SystemExit(main(1))
