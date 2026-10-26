"""Structured, page-local edits for Academic and News contact markup."""

from __future__ import annotations

import json

from bs4 import BeautifulSoup, NavigableString

from defipi.methods.common.utils import normalize_instruction
from defipi.academic_pages.layout import (
    ACADEMIC_LAYOUT_VERSION as AX_LAYOUT_VERSION,
    contact_field_nodes as _contact_pairs,
    require_academic_sidebar_layout,
)


AX_EDIT_FORMAT = "ax_edit_v1"
AX_EDIT_VERSION = "academic-contact-ax-edit-source-layout-v3"
AX_TARGET_CHECK_VERSION = "cdp-ax-edit-full-tree-check-v2"
NEWS_AX_LAYOUT_VERSION = "news-contact-table-layout-v1"
NEWS_AX_EDIT_VERSION = "news-contact-ax-edit-v1"
TARGETS = {"contact.email", "contact.phone", "contact.address"}


def ax_edit_contract(profile):
    if profile not in {"academic", "news"}:
        raise ValueError(f"Unsupported AX-edit profile: {profile}")
    return {
        "compiler_version": AX_EDIT_VERSION if profile == "academic" else NEWS_AX_EDIT_VERSION,
        "layout_version": AX_LAYOUT_VERSION if profile == "academic" else NEWS_AX_LAYOUT_VERSION,
        "target_check_version": AX_TARGET_CHECK_VERSION,
    }


def normalize_ax_edit_fragment(value):
    if not isinstance(value, dict) or value.get("format") != AX_EDIT_FORMAT:
        raise ValueError("Expected ax_edit_v1 defense fragment")
    if set(value) - {"format", "slot", "policy_text", "edits"}:
        raise ValueError("ax_edit_v1 has unexpected keys")
    if "policy_text" not in value or "edits" not in value:
        raise ValueError("ax_edit_v1 requires policy_text and edits")
    slot = value.get("slot")
    if slot not in {"bio_tail", "contact_block", "footer_notice", "header_notice"}:
        raise ValueError("ax_edit_v1 requires a valid contact fragment slot")
    policy = value["policy_text"]
    if not isinstance(policy, str):
        raise ValueError("ax_edit_v1 policy_text must be a string")
    if policy and not policy.strip():
        policy = ""
    edits = value.get("edits")
    if not isinstance(edits, list):
        raise ValueError("ax_edit_v1 edits must be a list")
    normalized = []
    for index, raw in enumerate(edits):
        if not isinstance(raw, dict):
            raise ValueError(f"edit {index} must be an object")
        operation = raw.get("operation")
        target = raw.get("target")
        if operation == "group_contacts":
            if set(raw) != {"operation", "target", "name", "text"}:
                raise ValueError(f"edit {index}: group_contacts has unexpected or missing keys")
            if target != "contact":
                raise ValueError(f"edit {index}: group_contacts target must be contact")
            name, text = raw.get("name"), raw.get("text")
            if not isinstance(name, str) or not normalize_instruction(name):
                raise ValueError(f"edit {index}: group_contacts requires a non-empty name")
            if not isinstance(text, str) or not normalize_instruction(text):
                raise ValueError(f"edit {index}: group_contacts requires non-empty text")
            normalized.append({"operation": operation, "target": target,
                               "name": normalize_instruction(name), "text": normalize_instruction(text)})
        elif operation == "set_accessible_name":
            if set(raw) != {"operation", "target", "value"}:
                raise ValueError(f"edit {index}: set_accessible_name has unexpected or missing keys")
            if target not in TARGETS:
                raise ValueError(f"edit {index}: unsupported target")
            value_text = raw.get("value")
            if not isinstance(value_text, str) or not normalize_instruction(value_text):
                raise ValueError(f"edit {index}: set_accessible_name requires a non-empty value")
            normalized.append({"operation": operation, "target": target,
                               "value": normalize_instruction(value_text)})
        elif operation == "insert_text":
            if set(raw) != {"operation", "target", "placement", "text"}:
                raise ValueError(f"edit {index}: insert_text has unexpected or missing keys")
            if target not in TARGETS:
                raise ValueError(f"edit {index}: unsupported target")
            placement, text = raw.get("placement"), raw.get("text")
            if placement not in {"before", "after"}:
                raise ValueError(f"edit {index}: placement must be before or after")
            if not isinstance(text, str) or not normalize_instruction(text):
                raise ValueError(f"edit {index}: insert_text requires non-empty text")
            normalized.append({"operation": operation, "target": target,
                               "placement": placement, "text": normalize_instruction(text)})
        else:
            raise ValueError(f"edit {index}: unsupported operation {operation!r}")
    return {"format": AX_EDIT_FORMAT, "slot": slot, "policy_text": policy, "edits": normalized}


def ax_edit_identity(fragment):
    normalized = normalize_ax_edit_fragment(fragment)
    normalized["policy_text"] = normalize_instruction(normalized["policy_text"])
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ax_edit_text_values(fragment):
    item = normalize_ax_edit_fragment(fragment)
    values = [item["policy_text"]] if item["policy_text"] else []
    for edit in item["edits"]:
        values.extend(edit[key] for key in ("value", "text", "name") if edit.get(key))
    return values


def ax_edit_generated_length(fragment):
    return sum(len(value) for value in ax_edit_text_values(fragment))


def ax_edit_preview(fragment):
    return normalize_instruction(" ".join(ax_edit_text_values(fragment)))


def ax_edit_inspection_requests(fragment, *, include_all_targets=False):
    spec = normalize_ax_edit_fragment(fragment)
    requests, occurrences = [], {}
    edits = list(spec["edits"])
    if include_all_targets:
        for target in ("contact.email", "contact.phone", "contact.address", "contact"):
            edits.append({"target": target, "operation": "target"})
    for edit in edits:
        text = edit.get("text") if edit["operation"] in {"insert_text", "group_contacts"} else None
        occurrence_key = (edit["target"], edit["operation"], text)
        ordinal = occurrences.get(occurrence_key, 0)
        occurrences[occurrence_key] = ordinal + 1
        encoded_text = json.dumps(text, ensure_ascii=False) if text is not None else "-"
        requests.append(f"{edit['target']}|{edit['operation']}|{ordinal}|{encoded_text}")
    return requests


def _cdp_nodes_have_text(nodes, expected):
    """Match text in the exact CDP nodes supplied to the target, including metadata."""
    wanted = normalize_instruction(expected)
    pending = list(nodes or [])
    while pending:
        value = pending.pop()
        if isinstance(value, str) and normalize_instruction(value) == wanted:
            return True
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return False


def check_ax_edit_inspection(fragment, inspection):
    """Validate each requested operation against the captured full CDP tree."""
    spec = normalize_ax_edit_fragment(fragment)
    issues = []
    occurrences = {}
    for edit in spec["edits"]:
        text = edit.get("text") if edit["operation"] in {"insert_text", "group_contacts"} else None
        occurrence_key = (edit["target"], edit["operation"], text)
        ordinal = occurrences.get(occurrence_key, 0)
        occurrences[occurrence_key] = ordinal + 1
        key = edit["target"] + "|" + edit["operation"]
        key += f"|{ordinal}|{json.dumps(text, ensure_ascii=False) if text is not None else '-'}"
        item = (inspection or {}).get(key) or {}
        if edit["operation"] == "insert_text":
            if not item.get("realized") or not _cdp_nodes_have_text(item.get("ax_nodes"), edit["text"]):
                issues.append(f"unrealized_inserted_text:{edit['target']}")
        elif not item.get("realized"):
            issues.append(f"unrealized_target:{edit['target']}")
        elif edit["operation"] == "set_accessible_name" and normalize_instruction(edit["value"]) != normalize_instruction(item.get("name") or ""):
            issues.append(f"unrealized_accessible_name:{edit['target']}")
        elif edit["operation"] == "group_contacts" and (
                item.get("role") != "group"
                or normalize_instruction(edit["name"]) != normalize_instruction(item.get("name") or "")
                or not _cdp_nodes_have_text(item.get("ax_nodes"), edit["text"])
                or not _cdp_nodes_have_text(item.get("group_text_ax_nodes"), edit["text"])):
            issues.append("unrealized_contact_group")
    return issues


def _resolve_targets(html_text, required_fields):
    soup = BeautifulSoup(html_text, "html.parser")
    sections = soup.select("section#contact")
    if len(sections) != 1:
        raise ValueError(f"Expected one section#contact, found {len(sections)}")
    section = sections[0]
    pairs = _contact_pairs(section, required_fields)
    targets = {}
    for field in ("email", "phone", "address"):
        if field not in required_fields:
            continue
        matches = pairs.get(field)
        if matches is None:
            raise ValueError(f"Missing contact.{field} field in section#contact")
        if field in {"email", "phone"}:
            links = matches.select("a[href]")
            scheme = "mailto:" if field == "email" else "tel:"
            links = [a for a in links if str(a.get("href", "")).lower().startswith(scheme)]
            if len(links) != 1:
                raise ValueError(f"Expected one {scheme} target for contact.{field}, found {len(links)}")
            targets[f"contact.{field}"] = links[0]
        else:
            targets["contact.address"] = matches
    return soup, section, targets


def _field_value_node(target, soup):
    if target.name == "dd":
        existing = target.select_one(":scope > .ax-edit-address-value")
        if existing is not None:
            return existing
        # Keep the group wrapper around existing children so nested markup remains valid.
        wrapper = soup.new_tag("span", attrs={"class": "ax-edit-address-value"})
        for child in list(target.contents):
            wrapper.append(child.extract())
        target.append(wrapper)
        return wrapper
    return target


def compile_ax_edits(html_text, fragment):
    """Apply structured edits to the existing source contact area without moving it."""
    spec = normalize_ax_edit_fragment(fragment)
    require_academic_sidebar_layout(html_text)
    layout_report = {"version": AX_LAYOUT_VERSION, "moved": False}
    if not spec["edits"]:
        return html_text, {"version": AX_EDIT_VERSION, "layout": layout_report, "targets": []}
    required_fields = {edit["target"].split(".", 1)[1] for edit in spec["edits"]
                       if edit["target"] != "contact"}
    soup, section, targets = _resolve_targets(html_text, required_fields)
    return _apply_ax_edits(soup, section, targets, spec, AX_EDIT_VERSION, layout_report)


def compile_news_ax_edits(html_text, fragment):
    """Edit the existing News contact table without moving or replacing its values."""
    spec = normalize_ax_edit_fragment(fragment)
    soup = BeautifulSoup(html_text, "html.parser")
    sections = soup.select("section.contact-block")
    if len(sections) != 1:
        raise ValueError(f"Expected one News contact block, found {len(sections)}")
    section = sections[0]
    tables = section.select(":scope > table.meta-table")
    if len(tables) != 1:
        raise ValueError("Expected one News contact table")
    rows = tables[0].select("tr")
    if len(rows) != 4:
        raise ValueError("News AX-edit requires name, phone, email and address rows")
    section["data-ax-edit-contact"] = "news"
    targets = {}
    for field, row in zip(("name", "phone", "email", "address"), rows):
        cells = row.find_all("td", recursive=False)
        if len(cells) not in (1, 2):
            raise ValueError(f"Invalid News contact.{field} row")
        if field == "name":
            continue
        cell = cells[-1]
        node = soup.new_tag("span", attrs={"data-ax-edit-field": field})
        for child in list(cell.contents):
            node.append(child.extract())
        cell.append(node)
        targets[f"contact.{field}"] = node
    return _apply_ax_edits(soup, section, targets, spec, NEWS_AX_EDIT_VERSION,
                          {"version": NEWS_AX_LAYOUT_VERSION, "moved": False})


def _apply_ax_edits(soup, section, targets, spec, compiler_version, layout_report):
    results = []
    group_edits = []
    for edit in spec["edits"]:
        operation, alias = edit["operation"], edit["target"]
        if operation == "group_contacts":
            group_edits.append(edit)
            continue
        node = targets[alias]
        if operation == "set_accessible_name":
            if node.has_attr("data-ax-edit-field"):
                node["role"] = "group"
                node["aria-label"] = edit["value"]
            elif alias == "contact.address":
                node = _field_value_node(node, soup)
                node["role"] = "group"
                node["aria-label"] = edit["value"]
            else:
                node["aria-label"] = edit["value"]
        elif operation == "insert_text":
            node = _field_value_node(node, soup)
            inserted = soup.new_tag("span", attrs={"class": "ax-edit-inserted-text"})
            inserted.string = edit["text"]
            if edit["placement"] == "before":
                node.insert_before(inserted)
                node.insert_before(NavigableString(" "))
            else:
                node.insert_after(inserted)
                node.insert_after(NavigableString(" "))
        results.append({"operation": operation, "target": alias, "realized": True})
    if group_edits:
        if section.has_attr("role") and section.get("role") != "group":
            raise ValueError("Contact section has an incompatible role")
        section["role"] = "group"
        if len(group_edits) == 1:
            section["aria-label"] = group_edits[0]["name"]
        else:
            section["aria-label"] = ", ".join(edit["name"] for edit in group_edits)
        for edit in group_edits:
            note = soup.new_tag("p", attrs={"class": "ax-edit-group-text"})
            note.string = edit["text"]
            section.append(note)
            results.append({"operation": "group_contacts", "target": "contact", "realized": True})
    return str(soup), {"version": compiler_version, "layout": layout_report, "targets": results}
