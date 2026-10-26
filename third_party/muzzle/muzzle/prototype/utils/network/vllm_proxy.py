import asyncio
import itertools
import threading
import logging
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response
import uvicorn

class VLLMProxy:
    """
    Simple HTTP proxy that forwards requests to a target VLLM server
    and records a transcript of request / response pairs.

    Public API:
        start() -> None
        stop() -> List[Dict[str, Any]]   # transcript
    """

    def __init__(
        self,
        target_base_url: str,
        host: str = "localhost",
        port: int = 7777,
        redact_repeated_system_prompts: bool = True
    ) -> None:
        self.target_base_url = target_base_url.rstrip("/")
        self.host = host
        self.port = port
        self.redact_repeated_system_prompts = redact_repeated_system_prompts
        self.logger = logging.getLogger(self.__class__.__name__)

        self._app = FastAPI()
        self._setup_routes()

        self._seq_counter = itertools.count(1)
        self._transcript: List[Dict[str, Any]] = []
        self._lock = threading.Lock()

        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    def _uvicorn_log_config(self) -> Dict[str, Any]:
        """Return a logging config that routes uvicorn logs to the parent logger/handlers.

        We remove uvicorn's own handlers and let logs propagate up so they get
        formatted by the already-configured parent logger (root or provided).
        """
        level_name = logging.getLevelName(self.logger.level)
        return {
            "version": 1,
            "disable_existing_loggers": False,
            # No formatters/handlers here; we rely on parent logger's handlers/formatters
            "formatters": {},
            "handlers": {},
            "loggers": {
                "uvicorn": {"handlers": [], "level": level_name, "propagate": True},
                "uvicorn.error": {"handlers": [], "level": level_name, "propagate": True},
                "uvicorn.access": {"handlers": [], "level": level_name, "propagate": True},
            },
        }

    def _simplify_transcript(self, transcript):
        out = []
        # Track unique system prompts in order of first appearance.
        # Maps system prompt content -> 1-based index of that unique prompt.
        seen_system_prompts: Dict[str, int] = {}

        for i, rec in enumerate(transcript):
            body = rec["request"]["json"] or {}
            messages = body.get("messages", [])

            # Filter request so that it does not include base64 encoded images.
            for msg in messages:
                if "content" in msg and isinstance(msg["content"], list):
                    msg["content"] = list(filter(lambda x: not ("image_url" in x.keys()), msg["content"]))

                if self.redact_repeated_system_prompts and msg.get("role") == "system" and isinstance(msg.get("content"), str):
                    prompt_text = msg["content"]
                    if prompt_text not in seen_system_prompts:
                        # First occurrence of this unique system prompt; keep it and record its index.
                        seen_system_prompts[prompt_text] = len(seen_system_prompts) + 1
                    else:
                        # Repeated occurrence; redact with an index suffix for the 2nd+ unique prompt.
                        idx = seen_system_prompts[prompt_text]
                        suffix = "" if idx == 1 else f" {idx}"
                        msg["content"] = f"[repeated system prompt{suffix}]"

            assistant_content = None
            resp = rec["response"]["json"] or {}
            if isinstance(resp, dict):
                # adapt as needed for your vLLM OpenAI-style output
                choices = resp.get("choices") or []
                if choices and "message" in choices[0]:
                    # assistant_content = choices[0]["message"].get("content")
                    assistant_content = choices[0] # TODO: <-- This will be required for agent-e.

            out.append({
                "seq": rec["seq"],
                "request": {
                    "url": rec["request"]["url"],
                    "json": messages,
                },
                "response": {
                    "status_code": rec["response"]["status_code"],
                    "reason": rec["response"]["reason"],
                    "json": {
                        "role": "assistant",
                        "content": assistant_content,
                    },
                },
            })
        return out

    def _reset(self) -> None:
        with self._lock:
            self._transcript.clear()
            self._seq_counter = itertools.count(1)

    def start(self) -> None:
        """Start the proxy in a background thread."""
        if self._thread is not None:
            return  # already running

        self.logger.info(
            f"Starting VLLMProxy on {self.host}:{self.port} -> {self.target_base_url}"
        )

        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=int(self.port),
            log_level="info",
            log_config=self._uvicorn_log_config(),
        )
        self._server = uvicorn.Server(config)

        def _run_server() -> None:
            asyncio.run(self._server.serve())

        self._thread = threading.Thread(target=_run_server, daemon=True)
        self._thread.start()

    def stop(self) -> List[Dict[str, Any]]:
        """
        Stop the proxy and return the recorded transcript
        as a Python list of dicts (JSON serializable).
        """
        if self._server is not None:
            self._server.should_exit = True

        if self._thread is not None:
            self._thread.join()
            self._thread = None

        # Simplify the transcript:
        simple_transcript = self._simplify_transcript(self._transcript)

        self.logger.info(
            f"Stopped VLLMProxy on {self.host}:{self.port}; recorded {len(simple_transcript)} exchanges"
        )

        # Clear transcript
        self._reset()

        return simple_transcript

    def _setup_routes(self) -> None:
        @self._app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
        async def proxy(request: Request, path: str) -> Response:
            return await self._handle_request(request, path)

    async def _handle_request(self, request: Request, path: str) -> Response:
        method = request.method
        query_string = request.url.query
        target_url = f"{self.target_base_url}/{path}"
        if query_string:
            target_url = f"{target_url}?{query_string}"

        # Read body once and try to decode as JSON
        raw_body = await request.body()
        try:
            req_json = await request.json()
        except Exception:
            req_json = None

        # Forward headers, but drop some hop-by-hop ones
        filtered_headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in {
                "host",
                "connection",
                "keep-alive",
                "proxy-authenticate",
                "proxy-authorization",
                "te",
                "trailer",
                "transfer-encoding",
                "upgrade",
            }
        }

        async with httpx.AsyncClient(timeout=None) as client:
            resp = await client.request(
                method=method,
                url=target_url,
                headers=filtered_headers,
                content=raw_body,
            )

        # Try to decode response JSON
        try:
            resp_json = resp.json()
        except Exception:
            resp_json = None

        seq_num = next(self._seq_counter)

        record: Dict[str, Any] = {
            "seq": seq_num,
            "request": {
                "method": method,
                "url": str(request.url),
                "headers": dict(request.headers),
                "json": req_json,
                "raw_body": raw_body.decode("utf-8", errors="replace") if raw_body else None,
            },
            "response": {
                "status_code": resp.status_code,
                "reason": resp.reason_phrase,
                "headers": dict(resp.headers),
                "json": resp_json,
                "text": resp.text,
            },
        }

        with self._lock:
            self._transcript.append(record)

        # Trace per-request forwarding at debug level to avoid noisy logs by default
        self.logger.debug(
            f"[{record['seq']}] {request.client.host if request.client else '-'} -> {method} {target_url} {(request.url.query or '')} => {resp.status_code}"
        )

        # Build response to the original client
        # Strip some headers that do not make sense to forward as is
        response_headers = {
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in {
                "content-encoding",
                "transfer-encoding",
                "connection",
            }
        }

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=response_headers,
            media_type=resp.headers.get("content-type"),
        )

# Example usage:
if __name__ == "__main__":
    proxy = VLLMProxy(target_base_url="http://localhost:7777", host="localhost", port=4949)
    proxy.start()
    print("Proxy started. Press Enter to stop...")
    input()
    transcript = proxy.stop()
    print("Proxy stopped. Transcript:")
    import json
    print(json.dumps(transcript, indent=2))
