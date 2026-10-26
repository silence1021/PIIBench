"""Repository root shared by source checkouts and packaged agent images."""
from pathlib import Path
import subprocess

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def git_revision():
    """Return the source commit and tracked working-tree state, if available."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, check=True,
            capture_output=True, text=True).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=PROJECT_ROOT, check=True, capture_output=True, text=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    return {"commit": commit, "dirty": bool(status.strip())}
