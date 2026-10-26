"""Serve the existing bound training pages for one local run."""
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import posixpath
import threading
from urllib.parse import unquote, urlparse


@contextmanager
def serve_bound_pages(config):
    settings = config["page_server"] if "page_server" in config else None
    if settings is None:
        yield None
        return
    url = urlparse(settings["base_url"])
    if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port:
        raise ValueError("page_server.base_url must be an explicit loopback HTTP URL and port")
    root = Path(settings["html_root"]).resolve()
    prefixes = {"/" + Path(row["html_file"]).parent.as_posix() + "/" for row in config["cases"]}
    for row in config["cases"]:
        path = (root / row["html_file"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"missing or out-of-root source page: {row['id']}")
        expected = settings["base_url"].rstrip("/") + "/" + row["html_file"]
        if row["starting_url"] != expected:
            raise ValueError(f"case URL differs from its configured source page: {row['id']}")

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send_head(self):
            path = posixpath.normpath(unquote(urlparse(self.path).path))
            if not any(path.startswith(prefix) or path + "/" == prefix for prefix in prefixes):
                self.send_error(404)
                return None
            return super().send_head()

    server = ThreadingHTTPServer((url.hostname, url.port), partial(Handler, directory=str(root)))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
