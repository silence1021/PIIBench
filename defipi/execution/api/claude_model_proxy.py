"""Count Claude's native Messages traffic and stop forwarding after a failed request."""
import json

from defipi.execution.agents.container import ContainerModelProxy


class ClaudeModelProxy(ContainerModelProxy):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stopped = None

    def record(self, row):
        super().record(row)
        if row["type"] == "response" and row["status"] >= 400:
            self.stopped = f"provider_http_{row['status']}"
        elif row["type"] in {"transport_error", "budget_stop"}:
            self.stopped = row["type"]
        elif row["type"] == "response_end":
            request = json.loads((self.directory / f"{row['index']:04d}.request.json").read_text())
            if request.get("request_kind") == "native_file_api":
                return
            path = self.directory / f"{row['index']:04d}.response.body"
            raw = path.read_text(errors="replace")
            # HTTP 200 may carry an Anthropic error event or a truncated stream.
            if raw.lstrip().startswith("{"):
                try:
                    body = json.loads(raw)
                    complete = body.get("type") == "message" and body.get("stop_reason")
                except ValueError:
                    complete = False
            else:
                events = []
                for line in raw.splitlines():
                    if line.startswith("data:"):
                        try:
                            events.append(json.loads(line[5:]))
                        except ValueError:
                            pass
                complete = any(e.get("type") == "message_stop" for e in events)
                complete = complete and not any(e.get("type") == "error" for e in events)
            if not complete:
                self.stopped = "incomplete_or_error_message_stream"
                super().record({"type": "stream_error", "index": row["index"], "error": self.stopped})

    def forward(self, handler):
        # Some native auxiliary calls have their own SDK retry policy. Do not
        # let those consume another paid request after any provider failure.
        retry = handler.headers.get("x-stainless-retry-count", "0")
        if self.stopped or retry not in ("", "0"):
            self.record({"type": "retry_blocked", "reason": self.stopped or "sdk_retry",
                         "sdk_retry_count": retry})
            handler.send_error(400, "This trajectory does not retry provider requests")
            return
        super().forward(handler)
