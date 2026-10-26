"""Restricted local HTTP serving for synthetic snapshot replay."""

from __future__ import annotations

import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class SnapshotRequestHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()


def make_server(root: str | Path, host: str, port: int) -> ThreadingHTTPServer:
    directory = str(Path(root).expanduser().resolve())
    handler = functools.partial(SnapshotRequestHandler, directory=directory)
    return ThreadingHTTPServer((host, port), handler)
