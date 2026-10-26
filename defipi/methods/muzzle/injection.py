"""Pure publisher notice insertion shared by search and offline export."""
import copy
from bs4 import BeautifulSoup
from defipi.inputs.contact_pages import _contact_values
INSERTION_ADAPTER = "publisher-html-insertion-v1"

def _notice(soup, payload):
    if not isinstance(payload, str) or not payload.strip():
        raise ValueError("Payload must be a nonempty string")
    notice = soup.new_tag("aside", attrs={"data-muzzle-pii": "notice"})
    notice["style"] = "white-space:pre-wrap"
    paragraph = soup.new_tag("p")
    paragraph.string = payload
    notice.append(paragraph)
    return notice


_VOID_ELEMENTS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                  "param", "source", "track", "wbr"}
_RAW_TEXT_ELEMENTS = {"script", "style", "textarea", "title", "xmp", "iframe", "noembed", "noframes", "plaintext"}
_FLOW_CONTAINERS = {"body", "div", "main", "section", "article", "aside", "nav", "header", "footer",
                    "address", "blockquote", "figure", "figcaption", "li", "td", "th", "dd", "dt",
                    "details", "summary", "fieldset"}


def _v2_anchor(soup, surface):
    if surface.get("adapter_version") != INSERTION_ADAPTER:
        raise ValueError("Unknown insertion adapter")
    selector = surface.get("selector")
    nodes = soup.select(selector) if isinstance(selector, str) and selector else []
    if len(nodes) != 1:
        raise ValueError("The selected placement is missing or ambiguous")
    node = nodes[0]
    if node.name in _VOID_ELEMENTS | _RAW_TEXT_ELEMENTS:
        raise ValueError("The selected placement cannot contain or neighbor a notice")
    body = soup.body
    if body is None or not (node is body or any(parent is body for parent in node.parents)):
        raise ValueError("Insertion must stay inside document body content")
    placement = surface.get("placement")
    if placement not in {"before", "after", "prepend", "append"}:
        raise ValueError("Unsupported insertion placement")
    if placement in {"prepend", "append"}:
        if node.name not in _FLOW_CONTAINERS - {"body"}:
            raise ValueError("The selected element is not a flow-content container")
    else:
        parent = node.parent
        if getattr(parent, "name", None) not in _FLOW_CONTAINERS:
            raise ValueError("The selected element has no suitable sibling insertion point")
    return node, placement


def _insert_notice(html, surface, payload, *, page_profile):
    soup = BeautifulSoup(html, "html.parser")
    if soup.select("[data-muzzle-pii]"):
        raise ValueError("Expected an unmodified base page")
    contacts_before = _contact_values(soup, page_profile)
    hrefs_before = [a.get("href") for a in soup.select("a[href]")]
    original_tree = copy.deepcopy(soup)
    node, placement = _v2_anchor(soup, surface)
    notice = _notice(soup, payload)
    if placement == "before":
        node.insert_before(notice)
    elif placement == "after":
        node.insert_after(notice)
    elif placement == "prepend":
        node.insert(0, notice)
    else:
        node.append(notice)
    remaining = copy.deepcopy(soup)
    markers = remaining.select('[data-muzzle-pii="notice"]')
    if len(markers) != 1:
        raise ValueError("Notice marker is missing or ambiguous")
    markers[0].extract()
    if remaining != original_tree:
        raise ValueError("Insertion changed original page content or structure")
    if _contact_values(soup, page_profile) != contacts_before or [a.get("href") for a in soup.select("a[href]")] != hrefs_before:
        raise ValueError("Insertion changed original contact values or links")
    return str(soup)


def inject_notice(html, surface, payload, *, page_profile):
    """Render a versioned publisher insertion placement."""
    return _insert_notice(html, surface, payload, page_profile=page_profile)
