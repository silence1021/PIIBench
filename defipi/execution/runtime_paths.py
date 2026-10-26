"""Project-local native dependencies shared by the agent adapters."""

from defipi.paths import PROJECT_ROOT as _PACKAGE_ROOT
from pathlib import Path

ROOT = _PACKAGE_ROOT
NODE_MODULES = ROOT / "node_modules"

def executable(name):
    path = NODE_MODULES / ".bin" / name
    return str(path)
