"""Shared JSON serialization, atomic writes, and process-local cache stores."""
import hashlib
import json
import os
from pathlib import Path
import threading


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_key(value):
    if not isinstance(value, (bytes, str)):
        value = canonical_json(value)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def write_json(path, value, **dump_options):
    path = Path(path)
    options = {"ensure_ascii": False, "indent": 2, **dump_options}
    serialized = json.dumps(value, **options) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(serialized, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class JsonStore(dict):
    """A cache whose mutations and serialization share one reentrant lock.

    Use put for whole entries; hold lock while mutating nested entries.
    """

    def __init__(self, path=None):
        super().__init__()
        self.path = Path(path) if path is not None else None
        self.lock = threading.RLock()
        if self.path is not None:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    self.update(value)
            except (OSError, ValueError):
                pass

    def put(self, key, value, save=True):
        with self.lock:
            self[key] = value
            if save:
                self.save()

    def save(self):
        with self.lock:
            if self.path is not None:
                write_json(self.path, self)


_STORES = {}
_STORES_LOCK = threading.Lock()


def open_store(path):
    if not path:
        return JsonStore()
    path = Path(path).expanduser().resolve()
    with _STORES_LOCK:
        if path not in _STORES:
            _STORES[path] = JsonStore(path)
        return _STORES[path]
