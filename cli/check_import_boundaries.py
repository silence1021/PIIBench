"""Check that data and evaluation packages cannot reach method implementations.

This static check reads imports, including imports inside functions, without
importing project modules or initializing clients. Run from any directory with
``python -m cli.check_import_boundaries``.
"""
from collections import deque
import ast
from importlib.util import resolve_name
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTECTED_PACKAGES = (
    "evaluation", "execution", "configuration", "inputs", "benchmark", "artifacts",
    "academic_pages", "synthetic_personas", "snapshots", "generation", "pages",
)


def import_graph(root):
    root = Path(root)
    paths = {}
    for path in (root / "defipi").rglob("*.py"):
        parts = list(path.relative_to(root).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts.pop()
        paths[".".join(parts)] = path
    graph = {name: set() for name in paths}
    for name, path in paths.items():
        package = name if path.name == "__init__.py" else name.rpartition(".")[0]
        # Python executes parent package initializers as part of every import.
        parent = name.rpartition(".")[0]
        while parent:
            if parent in paths:
                graph[name].add(parent)
            parent = parent.rpartition(".")[0]
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            targets = []
            if isinstance(node, ast.Import):
                targets = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                module = "." * node.level + (node.module or "")
                absolute = resolve_name(module, package) if node.level else module
                targets = [absolute] + [absolute + "." + item.name for item in node.names]
            elif isinstance(node, ast.Call) and node.args:
                function = node.func
                if ((isinstance(function, ast.Name) and function.id in {"import_module", "__import__"})
                        or (isinstance(function, ast.Attribute) and function.attr == "import_module")):
                    argument = node.args[0]
                    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                        targets = [argument.value]
                    elif isinstance(argument, ast.JoinedStr) and argument.values and isinstance(argument.values[0], ast.Constant):
                        # A dynamic import with a fixed methods prefix is still
                        # a method dependency even when its suffix is variable.
                        targets = [str(argument.values[0].value).rstrip(".")]
            for target in targets:
                if target in paths or target == "defipi.methods" or target.startswith("defipi.methods."):
                    graph[name].add(target)
    return graph


def boundary_violations(root=ROOT):
    graph = import_graph(root)
    violations = []
    for source in sorted(graph):
        if not any(source == "defipi." + part or source.startswith("defipi." + part + ".")
                   for part in PROTECTED_PACKAGES):
            continue
        pending = deque([(source, [source])])
        seen = {source}
        while pending:
            current, route = pending.popleft()
            if current == "defipi.methods" or current.startswith("defipi.methods."):
                violations.append(route)
                break
            for target in sorted(graph.get(current, ())):
                if target not in seen:
                    seen.add(target)
                    pending.append((target, route + [target]))
    return violations


def main():
    violations = boundary_violations()
    for route in violations:
        print(" -> ".join(route))
    print(f"Import boundaries: {len(violations)} violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
