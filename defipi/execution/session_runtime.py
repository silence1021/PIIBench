"""Neutral execution directories with unmodified wire evidence and archive links."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import tempfile
import threading
import uuid
from defipi.jsonio import write_json

VERSION = "session-runtime-v2"
RUNTIME_CACHE_PATHS = ("tmp/node-compile-cache", "cache/pip", "browser/cache/fontconfig")


def workspace_mapping(evidence, work):
    write_json(Path(evidence) / "workspace-paths.json", {
        "runtime_root": str(work), "archive_root": str(Path(evidence) / "workspace-artifacts")})


def runtime_request(request, session_id):
    """Carry execution inputs, leaving dataset/method/condition bookkeeping outside."""
    keys = ("schema_version", "question", "input", "target", "tool_config", "limits",
            "url_prompt_defense")
    result = {key: request[key] for key in keys if key in request}
    result = json.loads(json.dumps(result))
    result["case_id"] = session_id
    result["experiment"] = {key: request["experiment"][key] for key in
                            ("harness_type", "mode", "attack_method", "web_injection_channel")
                            if key in request["experiment"]}
    return result


def session_environment(env, root):
    """Give native clients fresh homes without changing their tools or permissions."""
    root = Path(root)
    result = dict(env)
    for key, name in (("HOME", "home"), ("TMPDIR", "tmp"), ("XDG_CONFIG_HOME", "config"),
                      ("XDG_CACHE_HOME", "cache"), ("XDG_DATA_HOME", "data"), ("XDG_STATE_HOME", "state")):
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        result[key] = str(directory)
    # Parent-shell working-directory and Python import hints are irrelevant to
    # these Node/native clients and otherwise expose the invoking checkout.
    for key in ("PWD", "OLDPWD", "PYTHONPATH"):
        result.pop(key, None)
    return result


def runtime_script(source, directory):
    """Copy the small observer scripts, so native errors don't name the checkout."""
    source, directory = Path(source), Path(directory) / "runtime" / Path(source).parent.name
    directory.mkdir(parents=True, exist_ok=True)
    for sibling in source.parent.iterdir():
        if sibling.is_file() and sibling.suffix in {".js", ".mjs", ".cjs", ".ts"}:
            target = directory / sibling.name
            if not target.exists():
                shutil.copy2(sibling, target)
    return directory / source.name


def pi_package_directory(executable, fallback, directory, version):
    """Use Pi's supported package override with identical installed resources."""
    for candidate in (executable, fallback):
        if not candidate:
            continue
        for source in Path(candidate).resolve().parents:
            metadata = source / "package.json"
            if not metadata.is_file():
                continue
            package = json.loads(metadata.read_text())
            if package.get("name") != "@earendil-works/pi-coding-agent":
                continue
            if package.get("version") != version:
                raise ValueError("Pi resources differ from the bound CLI version")
            destination = Path(directory) / "runtime" / "package"
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns("node_modules"))
            return destination, source
    raise ValueError("Cannot locate the installed Pi package resources")


@contextmanager
def shared_budget(settings, case_id, *, container=False):
    """Keep the original exclusive-file counter on the host, out of agent context."""
    budget = settings.get("shared_request_budget")
    if not budget or "claim_url" in budget:
        yield
        return
    directory = Path(budget["directory"]).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    capability = secrets.token_hex(24)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/" + capability:
                self.send_error(403)
                return
            for index in range(budget["limit"]):
                try:
                    with (directory / str(index)).open("x") as stream:
                        # Same counter files and per-attempt attribution as before.
                        from datetime import datetime, timezone
                        json.dump({"case_id": case_id, "at": datetime.now(timezone.utc).isoformat()}, stream)
                    self.send_response(200)
                    self.end_headers()
                    return
                except FileExistsError:
                    continue
            self.send_error(429, "Request budget exhausted")

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("0.0.0.0" if container else "127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = "host.docker.internal" if container else "127.0.0.1"
    settings["shared_request_budget"] = {"limit": budget["limit"],
        "claim_url": f"http://{host}:{server.server_port}/{capability}"}
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def archive_references(value, source, destination):
    """Rebind derived file references only. Native text and tool I/O stay verbatim."""
    if isinstance(value, list):
        return [archive_references(item, source, destination) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    opaque = {"trace", "tools", "input", "output", "arguments", "content", "messages",
              "final_answer", "answer", "reasoning", "text", "error", "native_item"}
    for key, item in value.items():
        if key in opaque:
            result[key] = item
        elif isinstance(item, str) and (key.endswith(("_path", "_dir")) or key == "path"):
            prefix = str(source) + "/"
            result[key] = str(destination) + item[len(str(source)):] if item.startswith(prefix) else item
        elif key.endswith("_paths") and isinstance(item, list):
            result[key] = [str(destination) + path[len(str(source)):]
                           if isinstance(path, str) and path.startswith(str(source) + "/") else path for path in item]
        else:
            result[key] = archive_references(item, source, destination)
    return result


def export_session(source, destination):
    """Copy evidence without runtime caches and return this export's exclusions."""
    destination.mkdir(parents=True, exist_ok=True)
    special_files = []
    excluded_caches = []

    def ignore(directory, names):
        skipped = set()
        for name in names:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            relative = path.relative_to(source).as_posix()
            if relative in RUNTIME_CACHE_PATHS:
                files = size = 0
                if stat.S_ISREG(mode):
                    files, size = 1, path.lstat().st_size
                elif stat.S_ISDIR(mode):
                    for root, _, filenames in os.walk(path, followlinks=False):
                        for filename in filenames:
                            info = (Path(root) / filename).lstat()
                            if stat.S_ISREG(info.st_mode):
                                files += 1
                                size += info.st_size
                excluded_caches.append({"runtime_root": str(source), "path": relative,
                                        "files": files, "bytes": size})
                skipped.add(name)
            elif path == source / "runtime/package":
                skipped.add(name)
            elif not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                special_files.append(str(path.relative_to(source)))
                skipped.add(name)
        return skipped

    for item in source.iterdir():
        if item.name in ignore(source, [item.name]):
            continue
        target = destination / item.name
        if target.exists() or target.is_symlink():
            raise ValueError(f"Refusing to overwrite session artifact: {target.name}")
        if item.is_symlink():
            target.symlink_to(os.readlink(item))
        elif item.is_dir():
            # Native package resources are an execution copy of the installed
            # pinned dependency, not newly produced evidence. Keep their source
            # mapping rather than duplicating them in every attempt archive.
            shutil.copytree(item, target, symlinks=True, ignore=ignore)
        else:
            shutil.copy2(item, target)
    if special_files:
        special_path = destination / "runtime-special-files.json"
        report = json.loads(special_path.read_text()) if special_path.exists() else {
            "uncopied_special_files": [], "reason": "Transient OS sockets/devices have no file byte stream"}
        report["uncopied_special_files"] = list(dict.fromkeys(
            report["uncopied_special_files"] + special_files))
        exports = report.setdefault("exports", [])
        record = {"runtime_root": str(source), "uncopied_special_files": special_files}
        if record not in exports:
            exports.append(record)
        write_json(special_path, report)
    # Internal links (notably the counted provider directory) must survive the
    # removal of the runtime. Links to external files are retained as evidence.
    for path in destination.rglob("*"):
        if path.is_symlink():
            link = Path(os.readlink(path))
            if link.is_absolute() and link.is_relative_to(source):
                mapped = destination / link.relative_to(source)
                path.unlink()
                path.symlink_to(os.path.relpath(mapped, path.parent))
    # Only our derived indexes are rewritten. Preserve their exact runtime
    # versions alongside them; requests, events, tool returns and stderr aren't.
    for pattern in ("*-evidence/trajectory.json", "trace.json", "returned_images/index.json"):
        for path in destination.glob(pattern):
            raw = path.read_bytes()
            value = archive_references(json.loads(raw), source, destination)
            path.with_name(path.stem + ".runtime.json").write_bytes(raw)
            write_json(path, value)
    return excluded_caches


def isolated_runtime(function=None, *, container=False):
    """Use one neutral staging tree per attempt, including failed/retried attempts."""
    if function is None:
        return lambda adapter: isolated_runtime(adapter, container=container)
    @wraps(function)
    def run(request_path, response_path, *args, **kwargs):
        request_path, response_path = Path(request_path).resolve(), Path(response_path).resolve()
        from defipi.configuration.execution import validate_execution_request
        validate_execution_request(json.loads(request_path.read_text()), container=container)
        archive = response_path.parent
        archive.mkdir(parents=True, exist_ok=True)
        occupied = ("runtime-paths.json", "container-control.json", "pi-evidence", "codex-evidence",
                    "dsh-evidence", "claude-evidence")
        if response_path.exists() or any((archive / name).exists() for name in occupied):
            raise ValueError("Refusing to overwrite a session attempt")
        request = json.loads(request_path.read_text())
        session_id = uuid.uuid4().hex
        adapted = runtime_request(request, session_id)
        # Ignore a caller's experiment-named TMPDIR. Containers provide their
        # neutral writable root; host sessions use the system's plain /tmp.
        parent = "/work/sessions" if Path("/work/sessions").is_dir() else "/tmp"
        root = Path(tempfile.mkdtemp(prefix="session-", dir=parent)).resolve()
        exported = False
        retain_runtime = False
        try:
            runtime_response = root / "response.json"
            result = None
            with ExitStack() as stack:
                stack.enter_context(shared_budget(adapted["tool_config"], request.get("case_id"),
                    container=container))
                runtime_input = root / "runtime-request.json"
                write_json(runtime_input, adapted)
                try:
                    result = function(runtime_input, runtime_response, *args, **kwargs)
                finally:
                    # An unconfirmed container may still use its bind mount.
                    cleanup = root / "container-cleanup.json"
                    retain_runtime = (container and cleanup.exists() and
                        json.loads(cleanup.read_text()).get("container_removed") is False)
                    if retain_runtime and result is not None:
                        result["retained_runtime_root"] = str(root)
                    # Nested container sessions may already have a mapping. Keep
                    # both runtime namespaces resolvable from the final archive.
                    child = root / "runtime-paths.json"
                    child_metadata = json.loads(child.read_text()) if child.exists() else {}
                    mappings = child_metadata.get("mappings", [])
                    mappings += [json.loads(path.read_text()) for path in root.glob("*-evidence/workspace-paths.json")]
                    if result:
                        mappings += result.get("runtime_packages", [])
                    for row in mappings:
                        if row["archive_root"].startswith(str(root)):
                            row["archive_root"] = str(archive) + row["archive_root"][len(str(root)):]
                    mappings.append({"runtime_root": str(root), "archive_root": str(archive)})
                    # Response is a derived envelope; archive its raw form too.
                    if runtime_response.exists():
                        runtime_response.rename(root / "response.runtime.json")
                    exclusions = export_session(root, archive)
                    excluded_caches = []
                    for row in child_metadata.get("excluded_runtime_caches", []) + exclusions:
                        if row not in excluded_caches:
                            excluded_caches.append(row)
                    write_json(archive / "runtime-paths.json", {"version": VERSION, "session_id": session_id,
                        "case_id": request.get("case_id"), "mappings": mappings,
                        "wire_evidence": "unchanged", "execution_inputs": "runtime-request.json",
                        "excluded_runtime_caches": excluded_caches})
                    exported = True
            if result is not None:
                result = archive_references(result, root, archive)
                result.update(runtime_context_version=VERSION, runtime_paths_path=str(archive / "runtime-paths.json"))
                write_json(response_path, result)
        finally:
            if exported and not retain_runtime:
                shutil.rmtree(root)
            else:
                # A failed archive must never delete the only complete evidence.
                write_json(archive / "runtime-recovery.json", {"runtime_root": str(root), "archive_complete": exported,
                    "container_cleanup_unconfirmed": retain_runtime})
        return result
    return run
