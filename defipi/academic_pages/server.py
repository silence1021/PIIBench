"""Local-only preview serving for generated academic pages."""

from __future__ import annotations

import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class AcademicPageRequestHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()


def make_preview_server(
    build_dir: str | Path,
    *,
    host: str,
    port: int,
) -> ThreadingHTTPServer:
    root = Path(build_dir).expanduser().resolve()
    if not (root / "generation_manifest.json").is_file():
        raise ValueError(f"Not an academic page build: {root}")
    handler = functools.partial(AcademicPageRequestHandler, directory=str(root))
    return ThreadingHTTPServer((host, port), handler)
