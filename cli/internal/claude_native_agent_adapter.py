"""Run one Claude Code trajectory inside its own disposable Linux container."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from defipi.execution.agents.container import main

if __name__ == "__main__":
    main()
