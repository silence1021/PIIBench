"""Per-attempt official Playwright MCP transport shared by native agents."""
from __future__ import annotations

from defipi.paths import PROJECT_ROOT as _PACKAGE_ROOT

import json
import os
from pathlib import Path
from defipi.execution.session_runtime import runtime_script, session_environment
import re
import shutil
import signal
import socket
import subprocess
import time
from urllib.parse import urlsplit

ROOT = _PACKAGE_ROOT
RUNTIME = Path(__file__).with_name("pi_runtime")
PACKAGE = ROOT
MCP_VERSION = "0.0.80"


class PlaywrightMcpSession:
    def __init__(self, directory, *, startup_timeout_seconds, node=None, chrome=None):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.node = node or shutil.which("node")
        self.chrome = chrome
        self.startup_timeout_seconds = startup_timeout_seconds
        self.process = self.log = self.session = None
        self.closed = False

    def start(self):
        package = PACKAGE / "node_modules/@playwright/mcp"
        if json.loads((package / "package.json").read_text())["version"] != MCP_VERSION:
            raise ValueError(f"Playwright MCP {MCP_VERSION} is required; run npm ci in {PACKAGE}")
        (self.directory / "artifacts").mkdir(exist_ok=True)
        config = {
            "browser": {
                "browserName": "chromium", "isolated": True,
                "launchOptions": {"headless": True, **({"executablePath": self.chrome} if self.chrome else {})},
                "contextOptions": {"viewport": {"width": 1280, "height": 900}},
                "initPage": [str(runtime_script(RUNTIME / "mcp_capture.cjs", self.directory))],
            },
            "outputDir": str(self.directory / "artifacts"), "saveSession": True,
        }
        (self.directory / "config.json").write_text(json.dumps(config, indent=2) + "\n")
        command = [self.node, str(package / "cli.js"), "--config", str(self.directory / "config.json"),
                   "--host", "127.0.0.1", "--port", "0"]
        self.log = (self.directory / "server.log").open("wb")
        # Give each server its own process group; cleanup never targets other experiments.
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
                                        env=session_environment(dict(os.environ, PI_MCP_EVIDENCE=str(self.directory)), self.directory),
                                        cwd=self.directory, start_new_session=True)
        deadline = time.monotonic() + self.startup_timeout_seconds
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Playwright MCP startup failed; see {self.directory / 'server.log'}")
            match = re.search(r"Listening on (http://[^\s]+)", (self.directory / "server.log").read_text())
            if match:
                self.session = {"mcp_url": match[1] + "/mcp", "mcp_pid": self.process.pid,
                                "playwright_mcp_version": MCP_VERSION, "isolated": True}
                (self.directory / "session.json").write_text(json.dumps(self.session, indent=2) + "\n")
                return self
            time.sleep(0.05)
        raise TimeoutError("Playwright MCP startup timed out")

    def pi_config(self):
        return {"mcpServers": {"playwright": {"url": self.session["mcp_url"]}}}

    def close(self):
        if self.closed:
            return True
        records = self.directory / "navigation.jsonl"
        browser_pids = set()
        if records.exists():
            for line in records.read_text().splitlines():
                try:
                    row = json.loads(line)
                    if row.get("type") == "browser_started":
                        browser_pids.add(int(row["browser_pid"]))
                    elif row.get("type") == "browser_closed":
                        browser_pids.discard(int(row["browser_pid"]))
                except (ValueError, KeyError):
                    continue
        if self.process is not None:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            # Also reap descendants if Pi or the MCP server was interrupted mid-call.
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if self.log:
            self.log.close()
        for pid in browser_pids:
            try:
                # Playwright launches Chromium in its own process group.
                if os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        closed = True
        if self.session:
            with socket.socket() as sock:
                sock.settimeout(0.2)
                closed = sock.connect_ex(("127.0.0.1", urlsplit(self.session["mcp_url"]).port)) != 0
        remaining = set(browser_pids)
        for _ in range(30):
            for pid in list(remaining):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    remaining.remove(pid)
            if not remaining:
                break
            time.sleep(0.1)
        closed = closed and not remaining
        self.closed = closed
        (self.directory / "cleanup.json").write_text(json.dumps({"closed": closed, "browser_pids": sorted(browser_pids),
                                                                "remaining_browser_pids": sorted(remaining)}) + "\n")
        return closed


def start_browser(request, evidence):
    from .common import write_json
    settings = request["tool_config"]
    browser = PlaywrightMcpSession(evidence.parent / "browser", chrome=settings["chrome_executable"], startup_timeout_seconds=settings["mcp_startup_timeout_seconds"])
    try:
        browser.start()
        observer_path = evidence / "browser-config.json"
        write_json(observer_path, {
            "mcp_url": browser.session["mcp_url"], "mcp_sdk": settings["mcp_sdk"],
            "events_path": str(evidence / "browser-events.jsonl"),
            "tool_timeout_seconds": settings["mcp_tool_timeout_seconds"],
        })
        return browser, observer_path
    except Exception:
        browser.close()
        raise
