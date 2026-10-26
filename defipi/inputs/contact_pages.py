"""Shared Academic/News contact-page parsing; no defense is applied here."""
from bs4 import BeautifulSoup
from defipi.academic_pages.layout import require_academic_sidebar_layout

FIELDS = ("email", "phone", "address")
PAGE_PROFILES = ("academic", "news")

def validate_page_profile(page_profile: str) -> str:
    if page_profile not in PAGE_PROFILES:
        raise ValueError("page_profile must be academic or news")
    return page_profile


def contact_section(soup: BeautifulSoup, page_profile: str):
    validate_page_profile(page_profile)
    selector = "section#contact" if page_profile == "academic" else "section.contact-block"
    sections = soup.select(selector)
    if len(sections) != 1:
        raise ValueError(f"Expected one {selector}, found {len(sections)}")
    return sections[0]


def _news_contact_cells(soup: BeautifulSoup):
    section = contact_section(soup, "news")
    tables = section.select("table.meta-table-singlecol")
    if len(tables) != 1:
        raise ValueError("News contact block requires one single-column contact table")
    rows = tables[0].find_all("tr")
    cells = [row.find_all("td", recursive=False) for row in rows]
    if len(cells) != 4 or any(len(row) != 1 for row in cells):
        raise ValueError("News contact table requires four single-cell rows: name, phone, email, address")
    return dict(zip(("name", "phone", "email", "address"), (row[0] for row in cells)))


def contact_field_containers(soup: BeautifulSoup, page_profile: str):
    """Locate existing field containers without changing either page layout."""
    section = contact_section(soup, page_profile)
    if page_profile == "news":
        cells = _news_contact_cells(soup)
        return {field: cells[field] for field in FIELDS}
    labels = {"email": ("email", "e-mail"), "phone": ("phone", "telephone"),
              "address": ("address", "office", "location")}
    containers = {}
    for field in FIELDS:
        terms = [node for node in section.find_all("dt")
                 if " ".join(node.stripped_strings).strip().lower().rstrip(":") in labels[field]]
        if len(terms) != 1 or terms[0].find_next_sibling("dd") is None:
            raise ValueError(f"Expected one {field} definition with an adjacent dd")
        containers[field] = terms[0].find_next_sibling("dd")
    return containers


def _contact_cell_text(cell):
    return " ".join(" ".join(str(node).strip() for node in cell.find_all(string=True)
        if node.find_parent(class_="wc-semantic-context") is None).split())


def _contact_values(soup: BeautifulSoup, page_profile: str) -> dict[str, str]:
    """Read a unique contact definition list, refusing ambiguous fields."""
    validate_page_profile(page_profile)
    if page_profile == "news":
        values = {field: _contact_cell_text(cell)
                  for field, cell in contact_field_containers(soup, page_profile).items()}
        if not all(values.values()):
            raise ValueError("News contact fields must be nonempty")
        return values
    sections = soup.select("section#contact")
    if len(sections) != 1:
        raise ValueError(f"Expected one section#contact, found {len(sections)}")
    values: dict[str, str] = {}
    section = sections[0]
    for dt in section.find_all("dt"):
        label = " ".join(dt.stripped_strings).strip().lower().rstrip(":")
        field = next((f for f, aliases in {
            "email": ("email", "e-mail"), "phone": ("phone", "telephone"),
            "address": ("address", "office", "location"),
        }.items() if label in aliases), None)
        if field is None:
            continue
        dd = dt.find_next_sibling("dd")
        if dd is None:
            raise ValueError(f"Contact field {field} has no adjacent dd")
        if field in values:
            raise ValueError(f"Ambiguous repeated contact field: {field}")
        if field == "email":
            links = [a for a in dd.select("a[href]") if (a.get("href") or "").lower().startswith("mailto:")]
            if len(links) != 1:
                raise ValueError(f"Expected one mailto link, found {len(links)}")
            values[field] = links[0]["href"][7:].split("?", 1)[0].strip()
        elif field == "phone":
            links = [a for a in dd.select("a[href]") if (a.get("href") or "").lower().startswith("tel:")]
            if len(links) != 1:
                raise ValueError(f"Expected one tel link, found {len(links)}")
            values[field] = links[0]["href"][4:].split("?", 1)[0].strip()
        else:
            parts = []
            for node in dd.find_all(string=True):
                parent = node.parent
                if parent and ("wc-semantic-context" in (parent.get("class") or [])):
                    continue
                parts.append(str(node).strip())
            values[field] = " ".join(" ".join(parts).split())
    missing = [f for f in FIELDS if not values.get(f)]
    if missing:
        raise ValueError("Missing canonical contact field(s): " + ", ".join(missing))
    return values


def extract_page(html: str, seed: dict, *, page_profile: str) -> tuple[str, dict[str, str]]:
    validate_page_profile(page_profile)
    if page_profile == "academic":
        require_academic_sidebar_layout(html)
    soup = BeautifulSoup(html, "html.parser")
    values = _contact_values(soup, page_profile)
    expected = seed["reporter_info"]
    mismatch = [f for f in FIELDS if str(expected.get(f, "")).strip() != values[f]]
    if page_profile == "news" and _contact_cell_text(_news_contact_cells(soup)["name"]) != str(expected.get("name", "")).strip():
        mismatch.append("name")
    if mismatch:
        raise ValueError("Seed/contact mismatch for: " + ", ".join(mismatch))
    return html, values
