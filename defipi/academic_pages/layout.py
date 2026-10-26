"""Academic source-page layout, applied during page generation before any defense."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag


ACADEMIC_LAYOUT_VERSION = "academic-contact-single-sidebar-layout-v1"


def contact_field_nodes(section, required_fields):
    pairs = {}
    seen = set()
    dts = section.find_all("dt")
    dds = section.find_all("dd")
    for dt in dts:
        label = " ".join(dt.get_text(" ", strip=True).split()).lower()
        field = {"email": "email", "e-mail": "email", "phone": "phone",
                 "telephone": "phone", "office": "address", "address": "address"}.get(label)
        if not field or field not in required_fields:
            continue
        if field in seen:
            raise ValueError(f"Ambiguous contact.{field} field in section#contact")
        seen.add(field)
        sibling = next((item for item in dt.next_siblings if isinstance(item, Tag)), None)
        if sibling is not None and sibling.name == "dd" and sibling in dds:
            pairs[field] = sibling
    return pairs


def _normalized_contact_href(value):
    """Normalize only href whitespace and URI scheme/identity casing."""
    text = re.sub(r"\s+", "", str(value or "")).strip()
    if ":" not in text:
        return text.casefold()
    scheme, value = text.split(":", 1)
    return scheme.casefold() + ":" + value.casefold()


def _normalized_contact_text(value):
    return " ".join(str(value or "").split()).casefold()


def _sidebar_li_text_without_icons(node):
    parts = []
    for string in node.strings:
        parent = string.parent
        omit = False
        while parent is not None and parent is not node:
            if (parent.name == "i" or parent.get("aria-hidden") == "true"
                    or set(parent.get("class") or []) & {"ax-edit-inserted-text", "ax-edit-group-text"}):
                omit = True
                break
            parent = parent.parent
        if not omit:
            parts.append(str(string))
    return " ".join(" ".join(parts).split())


def _contact_source_text(node):
    parts = []
    for string in node.strings:
        parent = string.parent
        omit = False
        while parent is not None and parent is not node:
            if set(parent.get("class") or []) & {"ax-edit-inserted-text", "ax-edit-group-text"}:
                omit = True
                break
            parent = parent.parent
        if not omit:
            parts.append(str(string))
    return " ".join(" ".join(parts).split())


def normalize_academic_sidebar_layout(html_text):
    """Move the canonical contact section into the sidebar, removing only exact duplicates.

    This helper is idempotent. It preserves the original contact nodes, values and hrefs;
    only same-value contact duplicates inside the author URL list are removed.
    """
    soup = BeautifulSoup(html_text, "html.parser")
    sections = soup.select("section#contact")
    if len(sections) != 1:
        raise ValueError(f"Expected one section#contact, found {len(sections)}")
    wrappers = soup.select(".sidebar .author__urls-wrapper")
    if len(wrappers) != 1:
        raise ValueError(f"Expected one .sidebar .author__urls-wrapper, found {len(wrappers)}")
    wrapper = wrappers[0]
    url_lists = wrapper.select("ul.author__urls")
    if len(url_lists) != 1:
        raise ValueError(f"Expected one ul.author__urls in sidebar wrapper, found {len(url_lists)}")
    section, url_list = sections[0], url_lists[0]
    pairs = contact_field_nodes(section, {"email", "phone", "address"})
    missing = [field for field in ("email", "phone", "address") if field not in pairs]
    if missing:
        raise ValueError("Missing canonical contact field(s) in section#contact: "
                         + ", ".join(f"contact.{field}" for field in missing))
    canonical = {}
    for field, scheme in (("email", "mailto:"), ("phone", "tel:")):
        links = [node for node in pairs[field].select("a[href]")
                 if _normalized_contact_href(node.get("href")).startswith(scheme)]
        if len(links) != 1:
            raise ValueError(f"Expected one canonical {scheme} link in section#contact, found {len(links)}")
        canonical[field] = _normalized_contact_href(links[0].get("href"))
    canonical["address"] = _normalized_contact_text(_contact_source_text(pairs["address"]))
    if not canonical["address"]:
        raise ValueError("Canonical contact.address is empty in section#contact")

    css = (
        ".sidebar .author__urls-wrapper > section#contact.ax-edit-sidebar-contact {"
        "display:block;position:static;width:100%;max-width:100%;box-sizing:border-box;"
        "white-space:normal !important;overflow:visible !important;overflow-wrap:anywhere;"
        "word-break:normal;text-overflow:clip;cursor:default;}"
        ".sidebar .author__urls-wrapper > section#contact.ax-edit-sidebar-contact * {"
        "white-space:normal !important;overflow:visible !important;overflow-wrap:anywhere;"
        "word-break:normal;text-overflow:clip;max-width:100%;}"
        "@media (max-width:57.8125em){.sidebar .author__urls-wrapper.ax-edit-single-sidebar-wrapper{"
        "display:block;width:100%;vertical-align:initial;}}"
    )
    matching_links = {
        field: [link for link in url_list.select("a[href]")
                if _normalized_contact_href(link.get("href")) == canonical[field]]
        for field in ("email", "phone")
    }
    matching_addresses = [row for row in url_list.find_all("li", recursive=False)
                          if _normalized_contact_text(_sidebar_li_text_without_icons(row))
                          == canonical["address"]]
    styles = soup.select("style[data-ax-edit-single-sidebar-layout]")
    already_moved = (section.parent is wrapper and
                     list(wrapper.children).index(section) > list(wrapper.children).index(url_list))
    already_marked = ("ax-edit-sidebar-contact" in (section.get("class") or [])
                      and "ax-edit-single-sidebar-wrapper" in (wrapper.get("class") or []))
    already_styled = (len(styles) == 1
                      and styles[0].get("data-ax-edit-single-sidebar-layout") == ACADEMIC_LAYOUT_VERSION
                      and styles[0].get_text() == css)
    if (already_moved and already_marked and already_styled
            and not any(matching_links.values()) and not matching_addresses):
        return html_text, {"version": ACADEMIC_LAYOUT_VERSION, "moved": False,
                           "removed_sidebar_duplicates": {"email": 0, "phone": 0, "address": 0}}

    removed = {"email": 0, "phone": 0, "address": 0}
    for field, scheme in (("email", "mailto:"), ("phone", "tel:")):
        for link in matching_links[field]:
            if link.parent is None:
                continue
            row = link.find_parent("li")
            other_links = [item for item in row.select("a[href]") if item is not link] if row is not None else []
            if row is not None and row.parent is url_list and not other_links:
                row.decompose()
            else:
                link.decompose()
            removed[field] += 1
    for row in matching_addresses:
        if row.parent is url_list:
            row.decompose()
            removed["address"] += 1

    # Move the source subtree itself; never clone or rewrite its contact text/hrefs.
    section.extract()
    classes = list(section.get("class") or [])
    if "ax-edit-sidebar-contact" not in classes:
        classes.append("ax-edit-sidebar-contact")
    section["class"] = classes
    wrapper_classes = list(wrapper.get("class") or [])
    if "ax-edit-single-sidebar-wrapper" not in wrapper_classes:
        wrapper_classes.append("ax-edit-single-sidebar-wrapper")
    wrapper["class"] = wrapper_classes
    wrapper.append(section)

    style = styles[0] if styles else None
    for duplicate in styles[1:]:
        duplicate.decompose()
    if style is None:
        head = soup.head
        if head is None:
            head = soup.new_tag("head")
            if soup.html is not None:
                soup.html.insert(0, head)
            else:
                soup.insert(0, head)
        style = soup.new_tag("style", attrs={"data-ax-edit-single-sidebar-layout": ACADEMIC_LAYOUT_VERSION})
        head.append(style)
    style["data-ax-edit-single-sidebar-layout"] = ACADEMIC_LAYOUT_VERSION
    style.string = css
    return str(soup), {"version": ACADEMIC_LAYOUT_VERSION, "moved": True, "removed_sidebar_duplicates": removed}


def require_academic_sidebar_layout(html_text):
    """Check source layout without moving nodes or rewriting the input HTML."""
    soup = BeautifulSoup(html_text, "html.parser")
    contacts = soup.select("section#contact")
    wrappers = soup.select(".sidebar .author__urls-wrapper")
    if (len(contacts) != 1 or len(wrappers) != 1
            or contacts[0].parent is not wrappers[0]
            or not soup.select_one(
                f'style[data-ax-edit-single-sidebar-layout="{ACADEMIC_LAYOUT_VERSION}"]')):
        raise ValueError("Academic source must already use the single-sidebar contact layout; "
                         "regenerate the source pages before preparing an experiment")
