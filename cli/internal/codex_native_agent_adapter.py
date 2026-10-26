"""Run Codex CLI with native tools and no added MCP server."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from defipi.execution.agents.codex import main

if __name__ == "__main__":
    raise SystemExit(main())
