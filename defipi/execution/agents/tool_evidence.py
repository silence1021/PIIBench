"""Read native Codex evidence without supplying or replacing any tools."""
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit

ACCESS_POLICY = "native-tools-post-run-audit-v1"


def content_blocks(value):
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        value = value["content"]
    if isinstance(value, list):
        return [{**item, "type": {"input_text": "text", "output_text": "text", "input_image": "image"}.get(
                    item.get("type"), item.get("type"))} if isinstance(item, dict)
                else {"type": "text", "text": str(item)} for item in value]
    return [{"type": "text", "text": value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}]


def native_wire_trace(evidence):
    """Pair Responses tool calls with outputs in subsequent native inputs."""
    calls, outputs = {}, {}
    directory = Path(evidence) / "provider"
    for path in sorted(directory.glob("*.response.body")):
        for line in path.read_text(errors="replace").splitlines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except ValueError:
                continue
            items = ([event["item"]] if isinstance(event.get("item"), dict) else [])
            items += (event.get("response") or {}).get("output", [])
            for item in items:
                if item.get("type") not in {"function_call", "custom_tool_call", "web_search_call",
                                             "computer_call", "local_shell_call", "shell_call"}:
                    continue
                key = item.get("call_id") or item.get("id")
                if key:
                    calls[key] = {**calls.get(key, {}), **item, "evidence_path": str(path)}
    for path in sorted(directory.glob("*.request.json")):
        body = json.loads(path.read_text())
        for item in body.get("input", []) if isinstance(body.get("input"), list) else []:
            if isinstance(item, dict) and item.get("type", "").endswith("_call_output") and item.get("call_id"):
                outputs[item["call_id"]] = {"value": item.get("output"), "evidence_path": str(path)}
    trace = []
    for call_id, call in calls.items():
        output = outputs.get(call_id)
        observed = output is not None or call["type"] == "web_search_call"
        arguments = call.get("arguments", call.get("input", call.get("action", {})))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                pass
        trace.append({"type": "tool", "name": call.get("name") or call["type"], "call_id": call_id,
                      "input": arguments, "output": {"content": content_blocks(output["value"] if output else call) if observed else []},
                      "output_observed": observed,
                      "is_error": call.get("status") in {"failed", "incomplete"},
                      "native_item": call, "evidence_path": call["evidence_path"],
                      "output_evidence_path": output["evidence_path"] if output else call["evidence_path"],
                      "source": "native_responses_tool"})
    return trace


def access_audit(trace, working_directory, target):
    """Index native accesses after execution; never enforce an access boundary."""
    root = Path(working_directory) if working_directory else None
    calls, paths, review = [], [], []

    def explicit_paths(value, field="input"):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"path", "file_path", "filename", "cwd", "workdir"} and isinstance(child, str):
                    yield field + "." + key, child
                else:
                    yield from explicit_paths(child, field + "." + key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from explicit_paths(child, f"{field}[{index}]")

    for index, tool in enumerate(trace):
        item = tool.get("native_item") or {}
        row = {"trace_index": index, "tool": tool.get("name"), "call_id": tool.get("call_id"),
               "source": tool.get("source"), "input": tool.get("input"),
               "reported_error": tool.get("is_error"), "native_status": item.get("status"),
               "exit_code": item.get("exit_code"), "output_observed": tool.get("output_observed", True),
               "event_line": tool.get("event_line"), "evidence_path": tool.get("evidence_path"),
               "output_evidence_path": tool.get("output_evidence_path")}
        calls.append(row)
        for field, requested in explicit_paths(tool.get("input")):
            # Lexical resolution describes an argument, not proof that a file was
            # read. Do not dereference paths or inspect additional file content.
            path = Path(requested)
            absolute = None
            if not requested.startswith("~") and "://" not in requested and (path.is_absolute() or root):
                absolute = Path(os.path.normpath(path if path.is_absolute() else root / path))
            paths.append({"trace_index": index, "field": field, "requested_path": requested,
                          "absolute_path": str(absolute) if absolute else None,
                          "outside_working_directory": not absolute.is_relative_to(root) if absolute and root else None,
                          "basis": "explicit_tool_argument; lexical_path_only"})
        name = str(tool.get("name", ""))
        if (isinstance(tool.get("input"), str) or any(key in name for key in
                ("exec", "shell", "code", "computer", "browser")) or not row["output_observed"]):
            review.append(index)
    return {"policy": ACCESS_POLICY, "enforcement": "none", "affects_run_status": False,
            "working_directory": working_directory, "target_url": target,
            "target_url_evidence": target_url_evidence(trace, target),
            "tool_records": calls, "explicit_path_arguments": paths,
            "outside_working_directory_arguments": [p for p in paths if p["outside_working_directory"] is True],
            "trace_indices_for_manual_review": review,
            "coverage": "Native CLI events and Responses calls/results are archived. Explicit paths are argument "
                        "observations, not proven file reads. Shell/code, redirects and indirect reads require review "
                        "of the original trace; this is not an OS-level access trace. CLI and wire records may overlap."}


def target_url_evidence(trace, target):
    """Find native target-URL observations, from the recorded tool trace."""
    def normalized(value):
        parsed = urlsplit(value.rstrip(".,;"))
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query, ""))

    expected = normalized(target)
    def mentions(value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return any(normalized(url) == expected for url in re.findall(r"https?://[^\s<>\"'\\)\]]+", text))

    matches = []
    for tool in trace:
        if tool.get("is_error"):
            continue
        item = tool.get("native_item") or {}
        action = item.get("action") or {}
        if (isinstance(action, dict) and action.get("type") == "open_page"
                and item.get("status") == "completed" and mentions(action.get("url", ""))):
            basis = "completed_native_open_page"
        elif mentions(tool.get("output", {})) and tool.get("name") != "web_search_call":
            basis = "target_url_in_native_tool_output"
        elif (tool.get("name") == "command_execution" and item.get("exit_code") == 0
              and item.get("aggregated_output", "").strip() and mentions(item.get("command", ""))
              and re.search(r"\b(?:curl|wget)\b", item.get("command", ""))):
            basis = "completed_native_http_command_with_output"
        else:
            continue
        matches.append({"call_id": tool.get("call_id"), "tool": tool.get("name"), "url": target, "basis": basis})
    return matches
