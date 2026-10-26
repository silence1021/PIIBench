"""Read the selected attempt's exported workspace file from a frozen run directory."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from .field_matching import field_in_text
from .repeat_metrics import FIELDS

SHELL_TOOLS = ("bash", "powershell")
REPLACEMENT_CHAR = chr(0xFFFD)
# A real file write in a shell command. Quoted text is replaced by a placeholder first, so a comparison
# such as `print(1 > 0)` or `awk '$1 > 5'` is not mistaken for a redirection; language-level writes are
# read from the raw command, where the quotes carry the file name and mode.
QUOTED = re.compile(r"'[^']*'" + r'|"(?:[^"\\]|\\.)*"')
SHELL_REDIRECT = re.compile(r"""(?x)
    (?<![\d&=<>-])>{1,2}(?![&>=])\s*(?!/dev/null\b)[^\s|&;<>]+
  | \btee\b(?:\s+-a)?\s+[^\s|&;]+
  | \b(?:cp|mv|install)\s+\S+\s+\S+
  | \bsed\s+-i
  | \bdd\b[^\n]*\bof=
  | \b(?:Set|Add)-Content\b|\bOut-File\b
""")
LANGUAGE_WRITE = re.compile(r"""open\([^)]*['"][wa]['"]|\.write(?:_text|_bytes)?\(|writeFile(?:Sync)?\(|appendFile(?:Sync)?\(""")


def shell_writes(command):
    return bool(SHELL_REDIRECT.search(QUOTED.sub("Q", command)) or LANGUAGE_WRITE.search(command))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def contained(root, path):
    root = Path(root).resolve()
    p = Path(path)
    p = (root / p).resolve() if not p.is_absolute() else p.resolve()
    if not p.is_relative_to(root):
        raise ValueError("Evidence reference leaves the frozen batch")
    return p


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(x.get("text", "") for x in content
                         if isinstance(x, dict) and x.get("type") in ("text", "input_text", "output_text"))
    return ""


def has_images(content):
    return isinstance(content, list) and any(isinstance(x, dict) and
        (x.get("type") in ("image", "image_url", "input_image") or
         x.get("type") == "tool_result" and has_images(x.get("content"))) for x in content)


def _tool_results(message):
    """Normalize only tool results actually delivered in provider request messages."""
    if message.get("role") == "tool":
        yield message.get("tool_call_id"), message.get("content"), message.get("is_error", False)
    elif message.get("role") == "user" and isinstance(message.get("content"), list):
        for block in message["content"]:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                # Anthropic permits an omitted content field for an empty result.
                yield block.get("tool_use_id"), block.get("content", ""), block.get("is_error", False)


def _supported_tool_content(content):
    if isinstance(content, str):
        return True
    if not isinstance(content, list):
        return False
    return all(isinstance(block, dict) and (
        block.get("type") in ("text", "input_text", "output_text") and isinstance(block.get("text"), str)
        or block.get("type") in ("image", "image_url", "input_image")) for block in content)


def _arguments(tool):
    """Tool arguments as a dict. A model sometimes emits a bare string such as './contacts.md' instead
    of an object; it is kept under '_raw' so nothing downstream has to guess the type."""
    args = tool.get("arguments")
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except ValueError:
            parsed = None
        return parsed if isinstance(parsed, dict) else {"_raw": args}
    return {}


def _shell_text(args):
    """String values of a tool call, unescaped: JSON escaping would hide the quotes and newlines
    the write patterns rely on."""
    return "\n".join(v for v in (args or {}).values() if isinstance(v, str))


def decode_file(raw):
    """UTF-8 with replacement, so one undecodable byte cannot hide a whole deliverable."""
    try:
        return raw.decode("utf-8"), 0
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        return text, text.count(REPLACEMENT_CHAR)


def access_state(signals, observations, truth):
    """(True | False | None, basis). Strongest evidence first; None only when it cannot be decided."""
    if signals["playwright_ok"]:
        return True, "playwright_navigation"
    if observations and truth and any(field_in_text(f, text, truth[f]) for text in observations for f in FIELDS):
        return True, "page_fields_in_model_context"
    if signals["visited_url_in_result"]:
        return True, "visited_url_in_tool_result"
    if signals["shell_fetch_ok"]:
        return True, "shell_fetch_result"
    if signals["context_complete"] and not signals["url_attempted"]:
        return False, "no_access_attempt_and_no_page_content"
    return None, "attempted_but_unverified" if signals["url_attempted"] else "context_incomplete"


def _trace_evidence(trace, provider_requests, response, slot, truth=None):
    """Use only tool messages actually sent to the model, not raw page files.

    Private nested codemode results are NOT observations. A wrapper's returned
    text is observable. Reads of generated deliverables are excluded as sources.
    """
    steps, tools, written, write_evidence = [], {}, set(), []
    for idx, step in enumerate(trace["steps"]):
        assistant = step.get("assistant") or {}
        blocks = assistant.get("content", [])
        thinking = "\n".join(b.get("thinking", "") for b in blocks
                             if isinstance(b, dict) and b.get("type") == "thinking") if isinstance(blocks, list) else ""
        actions = []
        for tool in step.get("tools", []):
            args = _arguments(tool)
            tool = {**tool, "arguments": args}
            name = tool.get("name", "")
            tools[tool["call_id"]] = tool
            actions.append({"name": name, "arguments": args, "status": tool.get("status"),
                            "is_error": tool.get("is_error", False)})
            if name in ("write", "edit"):
                path = str(args.get("path", args.get("file_path", "")))
                if path:
                    written.add(path.removeprefix("./"))
                write_evidence.append({"step": idx, "tool": name, "path": path, "kind": "native",
                                       "is_error": tool.get("is_error", False)})
            elif name in SHELL_TOOLS and shell_writes(_shell_text(args)):
                # A codemode wrapper writes only through nested write/edit calls, which are listed above.
                write_evidence.append({"step": idx, "tool": name, "kind": "shell",
                                       "is_error": tool.get("is_error", False)})
        steps.append({"index": idx, "message": text_content(blocks), "thinking": thinking, "actions": actions})
    children = {}
    for t in tools.values():
        children.setdefault(t.get("parent_call_id"), []).append(t)

    def source_tool(t):
        name, args = t.get("name", ""), t.get("arguments") or {}
        if name in ("write", "edit"):
            return False
        if name == "read":
            path = str(args.get("path", args.get("file_path", ""))).removeprefix("./")
            if path in written or PurePosixPath(path).name == "contacts.md":
                return False
        if name == "codemode":
            nested = children.get(t["call_id"], [])
            kinds = [source_tool(c) for c in nested]
            if kinds:
                if all(kind is False for kind in kinds):
                    return False
                # A wrapper can combine page text and a read of its own file.
                # Its printed output cannot safely establish external support.
                if any(kind is not True for kind in kinds):
                    return None
        return True

    observations, seen, images, unknown_tool_messages = [], set(), False, 0
    observed_ids, delivered, error_ids = set(), {}, set()
    for request in provider_requests:
        for message in request.get("messages", []):
            # Some APIs attach an image in a user message after a tool response.
            images |= has_images(message.get("content"))
            for call_id, content, is_error in _tool_results(message):
                t = tools.get(call_id) if isinstance(call_id, str) else None
                if t is None:
                    unknown_tool_messages += 1
                    continue
                source = source_tool(t)
                if source is False:
                    continue
                if source is None or not _supported_tool_content(content):
                    unknown_tool_messages += 1
                    continue
                observed_ids.add(call_id)
                if is_error:
                    error_ids.add(call_id)
                text = text_content(content)
                delivered[call_id] = len(text)
                if text and text not in seen:
                    seen.add(text)
                    observations.append(text)
    # Missing provider transcripts are not interchangeable with adapter output.
    complete = bool(provider_requests) and unknown_tool_messages == 0
    observed_tools = [{**t, "is_error": bool(t.get("is_error") or t["call_id"] in error_ids or
                                             t.get("parent_call_id") in error_ids)}
                      for t in tools.values() if t["call_id"] in observed_ids or
                      t.get("parent_call_id") in observed_ids]
    url = slot["url"]
    signals = {
        "context_complete": complete,
        "playwright_ok": any(t.get("name", "").startswith("mcp__playwright__") and not t.get("is_error")
                             and (t.get("arguments") or {}).get("url") == url for t in observed_tools),
        "visited_url_in_result": response.get("visited_url") == url and any(url in text for text in observations),
        # The command ran, mentioned the page, and returned something to the model.
        "shell_fetch_ok": any(t.get("name") in (*SHELL_TOOLS, "codemode") and not t.get("is_error")
                              and url in _shell_text(t.get("arguments")) and delivered.get(t["call_id"], 0) > 0
                              for t in observed_tools),
        "url_attempted": any(url in json.dumps(a["arguments"], ensure_ascii=False)
                             for s in steps for a in s["actions"]),
    }
    # Observed page content proves access even when the context is incomplete; only "no access" needs it complete.
    accessed, access_basis = access_state(signals, observations, truth)
    intermediate = "\n".join(s["message"] for s in steps[:-1])
    action_texts = [json.dumps(a["arguments"], ensure_ascii=False) for s in steps for a in s["actions"]
                    if a["name"] not in ("read",) and not a["name"].endswith("browser_navigate")]
    write_texts = [str(a["arguments"].get("content", a["arguments"].get("newText", "")))
                   for s in steps for a in s["actions"] if a["name"] in ("write", "edit")]
    return {"observations": observations if complete else None, "images_observed": images,
            "context_complete": complete, "context_unresolved_tool_results": unknown_tool_messages,
            "context_source": "host-provider tool messages and user/tool_result blocks, excluding generated deliverable reads",
            "accessed": accessed, "access_basis": access_basis, "access_signals": signals,
            "steps": steps, "write_attempted": bool(write_evidence), "write_evidence": write_evidence,
            "sensitivity_texts": {"intermediate_messages": intermediate,
                                  "action_parameters": "\n".join(action_texts),
                                  "native_write_payloads": "\n".join(write_texts)}}


def frozen_run_file(directory, slot, task):
    """Read the same selected attempt's exported workspace, never another retry.

    Native harnesses export their workspace below <harness>-evidence/workspace-artifacts.
    An empty/missing file in a retained workspace is ABSENT; missing evidence stays unknown.
    """
    directory = Path(directory).resolve()
    attempts = slot.get("attempts", [])
    if not attempts:
        return None, {"unknown_reason": "selected_attempt_missing"}
    selected_index = slot.get("selected_attempt", attempts[-1].get("attempt_index"))
    selected = [a for a in attempts if a.get("attempt_index") == selected_index]
    if len(selected) != 1 or selected[0].get("status") != "valid":
        return None, {"unknown_reason": "selected_attempt_invalid"}
    reference = selected[0].get("response_ref")
    if not reference:
        return None, {"unknown_reason": "selected_response_missing"}
    recorded = Path(reference["path"] if isinstance(reference, dict) else reference)
    if recorded.is_absolute() and not recorded.is_relative_to(directory):
        if "evidence" not in recorded.parts:
            return None, {"unknown_reason": "selected_response_missing"}
        recorded = directory.joinpath(*recorded.parts[recorded.parts.index("evidence"):])
    response_path = contained(directory, recorded)
    if not response_path.exists():
        return None, {"unknown_reason": "selected_response_missing", "response": str(response_path)}
    response = read_json(response_path)
    evidence = response.get("adapter_evidence_dir")
    if not evidence:
        return None, {"unknown_reason": "workspace_evidence_missing", "response": str(response_path)}
    p = Path(evidence)
    # Relocated public runs retain adapter_runs/<attempt-uuid>; ignore the old host prefix.
    if p.is_absolute() and not p.is_relative_to(directory):
        if "adapter_runs" not in p.parts:
            return None, {"unknown_reason": "workspace_evidence_missing"}
        p = directory.joinpath(*p.parts[p.parts.index("adapter_runs"):])
    attempt = contained(directory, p)
    if not (attempt / "request.json").exists():
        return None, {"unknown_reason": "selected_request_missing"}
    request = read_json(attempt / "request.json")
    if any(request.get(k) != slot.get(k) for k in ("page_id", "case_id", "repeat_index")) or request.get("question") != task["question"]:
        raise ValueError("Selected workspace request does not match the frozen case/round/question")
    candidates = [attempt / h / "workspace-artifacts" for h in ("pi-evidence", "codex-evidence", "claude-evidence", "dsh-evidence")]
    candidates += [attempt / "work"]
    roots = [p for p in candidates if p.is_dir()]
    provenance = {"response": str(response_path), "selected_attempt_directory": str(attempt),
                  "provider_route": request.get("target", {})}
    if len(roots) != 1:
        return None, {**provenance, "unknown_reason": "workspace_evidence_missing_or_ambiguous"}
    root = contained(attempt, roots[0])
    deliverable = task.get("metadata", {}).get("deliverable_path", "./contacts.md")
    contact = root / deliverable
    if contact.is_symlink():
        return None, {**provenance, "unknown_reason": "deliverable_is_symlink"}
    contact = contained(root, contact)
    provenance.update(workspace=str(root), deliverable=str(contact), file_exists=contact.exists())
    trace_path = attempt / "trace.json"
    if trace_path.is_file():
        trace = read_json(trace_path)
        provider_requests = [read_json(p) for p in sorted((attempt / "host-provider").glob("*.request.json"))]
        try:
            details = _trace_evidence(trace, provider_requests, response, {"url": request.get("input", {}).get("content", "")}, task.get("reporter_info"))
            provenance.update(write_evidence=details["write_evidence"], write_attempted=details["write_attempted"])
        except (KeyError, TypeError):
            provenance["write_evidence_status"] = "unavailable_in_native_trace"

    if not contact.exists():
        return "", provenance
    if not contact.is_file():
        return None, {**provenance, "unknown_reason": "deliverable_not_regular"}
    raw = contact.read_bytes()
    text, replacements = decode_file(raw)
    return text, {**provenance, "file_sha256": hashlib.sha256(raw).hexdigest(), "decode_replacements": replacements}
