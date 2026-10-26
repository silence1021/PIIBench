"""Check local dependencies, inputs and optional live credentials without model requests."""
import argparse
import importlib
from importlib import metadata
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MODULES = {"pyyaml": "yaml", "beautifulsoup4": "bs4", "Pillow": "PIL"}


def run_command(argv):
    result = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()[-1600:] or f"Exit {result.returncode}")
    return result.stdout.strip()


def check_environment(config_path=None, *, live=False, container_image=None, browser=False):
    checks = []

    def check(name, action, fix):
        try:
            detail = action()
            checks.append({"check": name, "status": "ok", "detail": "Validated" if isinstance(detail, dict) else str(detail)})
            return detail
        except (OSError, ValueError, RuntimeError, ImportError, TypeError, KeyError,
                subprocess.SubprocessError) as exc:
            checks.append({"check": name, "status": "fail", "detail": str(exc), "fix": fix})
            return None

    def require(condition, message, success):
        if not condition:
            raise ValueError(message)
        return success

    check("python", lambda: require(sys.version_info >= (3, 11), "Python 3.11+ is required", sys.version.split()[0]),
          "Create the virtual environment with Python 3.11 or newer.")
    for requirement in (ROOT / "requirements.txt").read_text().splitlines():
        package, version = requirement.split("==")

        def installed(package=package, version=version):
            actual = metadata.version(package)
            importlib.import_module(MODULES.get(package, package))
            return require(actual == version, f"Installed {actual}; expected {version}", actual)

        check(package, installed, "python -m pip install -e .")

    lock = None
    if config_path:
        def load_config():
            from defipi.evaluation.final_eval_dispatch import build_lock
            return build_lock(config_path)

        # Do not include the lock itself (or any credential value) in the report.
        lock = check("config and inputs", load_config,
                     "Use cli.prepare_benchmark, cli.export_artifact and cli.make_eval_config; fix the reported path or binding.")
        if lock:
            checks[-1]["detail"] = ("Configuration, tasks and HTTP(S) URLs validated" if lock["runtime"]["dispatch"]["axes"]["web_injection_channel"] == "url"
                                    else "Configuration, tasks, HTML and declared resources validated")
    if live and not config_path:
        check("live config", lambda: require(False, "--live requires --config", ""),
              "Pass the concrete config you intend to run.")

    image = container_image
    if lock:
        from defipi.evaluation.final_eval_dispatch import call_plan
        runtime = lock["runtime"]
        binding = runtime["dispatch"]
        adapter = binding.get("adapter") or {}
        settings = adapter.get("tool_config") or {}
        image = settings.get("container_image")
        if not image and binding["axes"]["mode"] == "agent":
            check("agent runtime", lambda: require(False, "Public agents require a Docker image", ""),
                  "Generate a new config with cli.make_eval_config and --container-image.")
        targets = list((runtime.get("targets") or {"target": runtime["target"]}).values())
        if runtime["filter"].get("enabled") and not binding.get("shared_filter"):
            targets.append(runtime["filter"])
        if image and binding["axes"]["harness_type"] == "dsh":
            from defipi.execution.agents.dsh import dsh_search_target
            search = check("DSH search binding", lambda: dsh_search_target({"target": runtime["target"],
                          "tool_config": settings}), "Check the search provider settings in the DSH template.")
            if search:
                targets.append(search)
                checks[-1]["detail"] = "Search provider binding validated"
        if live:
            from defipi.config import load_local_env
            load_local_env()
        for index, target in enumerate(targets, 1):
            backend = target.get("backend")
            # External command adapters may own their transport and credentials.
            if "client_settings" not in target:
                continue
            key = target["client_settings"].get("api_key_env")
            if not key:
                check(f"provider {index}", lambda: require(False, f"Unsupported live backend: {backend}", ""),
                      "Use openai, anthropic, deepseek or openrouter for a live config.")
            elif live:
                check(key, lambda key=key: require(bool(os.environ.get(key, "").strip()), f"{key} is missing", "Set (value hidden)"),
                      f"Set {key} in the environment or checkout .env; see .env.example.")
            else:
                checks.append({"check": key, "status": "skip", "detail": "Pass --live to check credential presence"})
    if image:
        check("docker daemon", lambda: run_command(["docker", "info", "--format", "{{.ServerVersion}}"]),
              "Install and start Docker.")
        check("container image", lambda: run_command(["docker", "image", "inspect", image, "--format", "{{.Id}}"]),
              "Build the shared agent image from containers/agents/Dockerfile and use its image tag.")
        check("container Chromium", lambda: run_command(["docker", "run", "--rm", "--entrypoint", "/usr/bin/chromium", image, "--version"]),
              "Rebuild the version-pinned agent image from containers/agents/Dockerfile.")
    if browser:
        check("Python Chromium", lambda: run_command([sys.executable, "-c",
              "from playwright.sync_api import sync_playwright\n"
              "with sync_playwright() as p:\n"
              "    b=p.chromium.launch(headless=True)\n"
              "    try:\n"
              "        b.new_page().goto('about:blank')\n"
              "        print(b.version)\n"
              "    finally: b.close()\n"]), "python -m pip install -r requirements-rendering.txt; python -m playwright install chromium")
    report = {"ready": all(item["status"] != "fail" for item in checks), "checks": checks,
              "model_requests": 0, "live_credentials_checked": live,
              "scope": "Local readiness only; provider access, quota and remote URLs are not contacted."}
    if lock:
        report["call_plan"] = call_plan(lock)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Concrete config made by prepare_example or make_eval_config")
    parser.add_argument("--live", action="store_true", help="Also require credentials; never contact a model")
    parser.add_argument("--container-image", help="Check Docker and the shared agent image ID without a config")
    parser.add_argument("--browser", action="store_true", help="Check the method optimization/page-rendering dependencies")
    args = parser.parse_args(argv)
    report = check_environment(args.config, live=args.live, container_image=args.container_image, browser=args.browser)
    print(json.dumps(report, indent=2))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
