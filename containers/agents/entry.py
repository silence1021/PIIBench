"""Run the packaged native adapter through the host's counted provider proxy."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from defipi.execution.api import native_model_proxy as native_model_proxy
from defipi.execution.runtime_paths import NODE_MODULES, executable


def main():
    # Codex native helper aliases need a writable path outside the system /tmp.
    tempfile.tempdir = "/work/sessions"
    control = json.loads(Path(sys.argv[1]).read_text())
    request_path = Path(control["request_path"])
    request = json.loads(request_path.read_text())
    harness = request["experiment"]["harness_type"]

    class HostProxy:
        def __init__(self, target, key, directory, limit, **kwargs):
            Path(directory).symlink_to(control["provider_evidence_dir"], target_is_directory=True)
            self.base_url = control["proxy_url"]

        def start(self):
            return self

        def close(self):
            pass

    native_model_proxy.NativeModelProxy = HostProxy
    settings = request["tool_config"]
    if request["input"]["kind"] == "url":
        settings["chrome_executable"] = "/usr/bin/chromium"
    if harness != "pi-agent":
        settings["mcp_sdk"] = str(NODE_MODULES / "@modelcontextprotocol/sdk")
    os.environ[request["target"]["client_settings"]["api_key_env"]] = "local-proxy-only"
    if harness == "pi-agent":
        from defipi.execution.agents import pi as adapter
        settings.update(pi_executable=executable("pi"), model_proxy_url=control["proxy_url"])
    elif harness == "codex":
        from defipi.execution.agents import codex as adapter
        settings["codex_executable"] = executable("codex")
    elif harness == "dsh":
        from defipi.execution.agents import dsh as adapter
        settings["dsh_modules"] = str(NODE_MODULES)
        os.environ[settings["dsh_search_target"]["client_settings"]["api_key_env"]] = "local-proxy-only"
    elif harness == "claude":
        from defipi.execution.agents import claude as adapter
        settings["claude_executable"] = executable("claude")
    else:
        raise ValueError("Expected Pi, Codex, DSH or Claude Code")
    adapted = request_path.with_name("container.request.json")
    adapted.write_text(json.dumps(request, indent=2) + "\n")
    browser_version = subprocess.run(["/usr/bin/chromium", "--version"],
        check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    Path(control["response_path"]).with_name("browser-version.txt").write_text(browser_version + "\n")
    response = adapter.run_adapter.__wrapped__(adapted, control["response_path"], container=True)
    response["browser_version"] = browser_version
    Path(control["response_path"]).write_text(json.dumps(response, indent=2) + "\n")


if __name__ == "__main__":
    main()
