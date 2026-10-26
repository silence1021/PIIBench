"""Count and archive native harness HTTP requests without replacing their loops.

Only bound model/search endpoints and the native DeepSeek Files API are forwarded.
API keys stay in the parent process. Request/response bodies (including actual
model inputs) are saved without authentication headers. All provider HTTP requests
share one serial request budget and are charged before sending.
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import threading

import httpx


class NativeModelProxy:
    def __init__(self, target, api_key, directory, max_requests, *, connect_timeout_seconds, preserve_native_parameters, native_search=None, native_files=False, listen_host="127.0.0.1"):
        self.target, self.api_key = target, api_key
        self.connect_timeout_seconds = connect_timeout_seconds
        self.preserve_native_parameters = preserve_native_parameters
        self.native_search, self.native_files = native_search, native_files
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_requests = max_requests
        self.count = 0
        self.forward_guard = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                with owner.forward_guard:
                    owner.forward(self)

            def do_GET(self):
                with owner.forward_guard:
                    owner.forward(self)

            def do_DELETE(self):
                with owner.forward_guard:
                    owner.forward(self)

        self.server = ThreadingHTTPServer((listen_host, 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}/v1"
        return self

    def record(self, row):
        with (self.directory / "events.jsonl").open("a") as handle:
            handle.write(json.dumps(row) + "\n")

    def upstream_headers(self, handler, target, api_key, kind, suffix, file_request):
        transport = target["client_settings"]
        headers = {"Accept-Encoding": "identity"}
        if not file_request:
            headers["Content-Type"] = "application/json"
        elif handler.headers.get("Content-Type"):
            headers["Content-Type"] = handler.headers["Content-Type"]
        headers.update(transport["request_headers"])
        if suffix == "/messages" or (file_request and target["api"] == "anthropic-messages"):
            headers.update({"x-api-key": api_key, "anthropic-version": handler.headers.get("anthropic-version", "2023-06-01")})
            if kind == "native_web_search":
                headers["Authorization"] = "Bearer " + api_key
                headers["Accept"] = "application/json"
            if handler.headers.get("anthropic-beta"):
                headers["anthropic-beta"] = handler.headers["anthropic-beta"]
        else:
            headers["Authorization"] = "Bearer " + api_key
        return headers

    def forward(self, handler):
        target, api_key, kind = self.target, self.api_key, "agent_model"
        suffix = {"openai-responses": "/responses", "openai-completions": "/chat/completions",
                  "anthropic-messages": "/messages"}[self.target["api"]]
        path = handler.path.split("?")[0]
        file_request = self.native_files and re.fullmatch(r"/v1/files(?:/[A-Za-z0-9_.-]+(?:/content)?)?", path)
        if self.native_search and path == "/v1/native-web-search/messages" and handler.command == "POST":
            target, api_key = self.native_search["target"], self.native_search["api_key"]
            suffix, kind = "/messages", "native_web_search"
        elif file_request and handler.command in {"GET", "POST", "DELETE"}:
            suffix, kind = handler.path.removeprefix("/v1"), "native_file_api"
        elif path != "/v1" + suffix or handler.command != "POST":
            self.record({"type": "request_rejected", "path": handler.path, "error": "endpoint_mismatch"})
            handler.send_error(400, "Endpoint is outside the model binding")
            return
        transport = target["client_settings"]
        try:
            raw_body = handler.rfile.read(int(handler.headers.get("Content-Length", 0)))
            body = None if file_request else json.loads(raw_body)
            if body is not None and body.get("model") != target["model"]:
                raise ValueError("Native request model does not match the bound target")
        except (ValueError, TypeError) as exc:
            self.record({"type": "request_rejected", "error": str(exc)})
            handler.send_error(400, str(exc))
            return
        if self.count >= self.max_requests:
            self.record({"type": "budget_stop", "reason": "model_request_budget_exhausted"})
            handler.send_error(429, "Model request budget exhausted")
            return
        self.count += 1
        index = self.count
        # Only provider-specific parameters explicitly frozen in the target may
        # augment a request; messages/tools/identity are owned by the harness.
        # Match Pi's explicit omissions before applying the shared model binding.
        if not file_request and not self.preserve_native_parameters:
            for key in transport["omit_parameters"]:
                body.pop(key, None)
            body.update(transport["request_parameters"])
            if target.get("temperature") is not None:
                body["temperature"] = target["temperature"]
            if target.get("max_response_tokens") is not None:
                body["max_output_tokens" if suffix == "/responses" else "max_tokens"] = target["max_response_tokens"]
        directory = self.directory
        directory.mkdir(exist_ok=True)
        request_path = directory / f"{index:04d}.request.json"
        if file_request:
            raw_path = self.directory / f"{index:04d}.request.body"
            raw_path.write_bytes(raw_body)
            request_path.write_text(json.dumps({"request_kind": kind, "method": handler.command,
                                               "path": handler.path, "body_path": str(raw_path),
                                               "content_type": handler.headers.get("Content-Type")}))
        else:
            request_path.write_text(json.dumps(body, ensure_ascii=False))
        upstream_url = transport["base_url"].rstrip("/") + suffix
        if not file_request and "?" in handler.path:
            upstream_url += "?" + handler.path.split("?", 1)[1]
        self.record({"type": "request", "index": index, "body_path": str(request_path),
                     "request_kind": kind, "method": handler.command,
                     "upstream_url": upstream_url})
        headers = self.upstream_headers(handler, target, api_key, kind, suffix, file_request)
        response_started = False
        try:
            with httpx.Client(timeout=httpx.Timeout(transport["request_timeout_seconds"], connect=self.connect_timeout_seconds), follow_redirects=False) as client:
                payload = {"content": raw_body} if file_request or self.preserve_native_parameters else {"json": body}
                with client.stream(handler.command, upstream_url,
                                   headers=headers, **payload) as upstream:
                    self.record({"type": "response", "index": index, "status": upstream.status_code,
                                 "request_kind": kind})
                    handler.send_response(upstream.status_code)
                    handler.send_header("Content-Type", upstream.headers.get("content-type", "application/json"))
                    handler.send_header("Connection", "close")
                    handler.end_headers()
                    response_started = True
                    with (directory / f"{index:04d}.response.body").open("wb") as out:
                        for chunk in upstream.iter_bytes():
                            out.write(chunk)
                            out.flush()
                            handler.wfile.write(chunk)
                            handler.wfile.flush()
                    self.record({"type": "response_end", "index": index})
        except Exception as exc:
            self.record({"type": "transport_error", "index": index, "error": str(exc)})
            if not response_started:
                handler.send_error(502, "Upstream transport failed; see adapter evidence")
        finally:
            handler.close_connection = True

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
