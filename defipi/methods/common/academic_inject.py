"""Experiment-time transforms for validated academic profile pages."""

from __future__ import annotations

from bs4 import BeautifulSoup

from defipi.methods.common.defense_fragments import normalize_fragment_spec, render_defense_fragment
from defipi.pages.injection import build_injection_snippet


_RAW_POSITIONS = {"meta", "after", "footer"}
_FRAGMENT_POSITIONS = {"bio_tail", "contact_block", "footer_notice", "header_notice"}
ACADEMIC_DEFENSE_POSITIONS = tuple(sorted(_RAW_POSITIONS | _FRAGMENT_POSITIONS))
_CAMOUFLAGE_STYLE = """
.page-note-camouflaged {
  color: var(--ipi-camouflage, var(--page-surface, #fff));
  background: var(--ipi-camouflage, var(--page-surface, #fff));
  margin: 0;
  padding: 0;
  border: 0;
  font-size: 1px;
  line-height: 1px;
  max-height: 1px;
  overflow: hidden;
  white-space: pre-wrap;
}
""".strip()


def _fragment_node(soup: BeautifulSoup, fragment_html: str):
    fragment_soup = BeautifulSoup(fragment_html, "html.parser")
    node = fragment_soup.find()
    if node is None:
        raise ValueError("Rendered defense fragment is empty.")
    return node


def _footer(soup: BeautifulSoup):
    return (
        soup.select_one("footer[role='contentinfo']")
        or soup.select_one(".page__footer footer")
        or soup.find("footer")
    )


def inject_academic_defense(
    html_text: str,
    *,
    position: str,
    attack_instruction: str,
    defense_fragment: dict | None = None,
) -> str:
    """Inject one frozen PIIGuard candidate into an academic homepage."""

    normalized_position = str(position or "").strip().lower()
    if normalized_position not in ACADEMIC_DEFENSE_POSITIONS:
        raise ValueError(f"Unsupported academic defense position: {position}")

    soup = BeautifulSoup(html_text, "html.parser")
    if defense_fragment is not None:
        fragment = normalize_fragment_spec(defense_fragment)
        if fragment["slot"] != normalized_position:
            raise ValueError(
                f"Defense fragment slot {fragment['slot']!r} does not match "
                f"experiment position {normalized_position!r}."
            )
        if fragment.get("format") == "ax_edit_v1":
            from defipi.methods.common.ax_edits import compile_ax_edits
            edited_html, _realization = compile_ax_edits(html_text, fragment)
            if not fragment["policy_text"]:
                return edited_html
            edited_soup = BeautifulSoup(edited_html, "html.parser")
            plain = {
                "format": "plain_text_v1",
                "slot": normalized_position,
                "policy_text": fragment["policy_text"],
            }
            node = _fragment_node(edited_soup, render_defense_fragment(plain))
            if normalized_position == "header_notice":
                anchor = edited_soup.find("header")
                if anchor is None:
                    if edited_soup.body is None:
                        raise ValueError("Academic homepage is missing <body> for a header notice.")
                    anchor = edited_soup.new_tag("header")
                    edited_soup.body.insert(0, anchor)
                anchor.append(node)
            elif normalized_position == "bio_tail":
                anchor = edited_soup.select_one("section#about")
                if anchor is None:
                    raise ValueError("Academic homepage is missing section#about.")
                anchor.insert_after(node)
            elif normalized_position == "contact_block":
                anchor = edited_soup.select_one("section#contact")
                if anchor is None:
                    raise ValueError("Academic homepage is missing section#contact.")
                anchor.append(node)
            else:
                anchor = _footer(edited_soup)
                if anchor is None:
                    raise ValueError("Academic homepage is missing a footer.")
                anchor.append(node)
            return str(edited_soup)
        node = _fragment_node(soup, render_defense_fragment(fragment))
        if normalized_position == "header_notice":
            anchor = soup.find("header")
            if anchor is None:
                if soup.body is None:
                    raise ValueError("Academic homepage is missing <body> for a header notice.")
                anchor = soup.new_tag("header")
                soup.body.insert(0, anchor)
            anchor.append(node)
        elif normalized_position == "bio_tail":
            anchor = soup.select_one("section#about")
            if anchor is None:
                raise ValueError("Academic homepage is missing section#about.")
            anchor.insert_after(node)
        elif normalized_position == "contact_block":
            anchor = soup.select_one("section#contact")
            if anchor is None:
                raise ValueError("Academic homepage is missing section#contact.")
            anchor.append(node)
        else:
            anchor = _footer(soup)
            if anchor is None:
                raise ValueError("Academic homepage is missing a footer.")
            anchor.append(node)
        return str(soup)

    instruction = str(attack_instruction or "").strip()
    if not instruction:
        raise ValueError("Academic defense injection requires a non-empty instruction.")
    if normalized_position == "meta":
        if soup.head is None:
            raise ValueError("Academic homepage is missing <head>.")
        tag = soup.new_tag("meta")
        tag["name"] = "page-note"
        tag["content"] = instruction
        soup.head.append(tag)
        return str(soup)

    if soup.head is None:
        raise ValueError("Academic homepage is missing <head>.")
    if soup.head.select_one("style[data-piiguard-camouflage]") is None:
        style = soup.new_tag("style")
        style["data-piiguard-camouflage"] = "true"
        style.string = _CAMOUFLAGE_STYLE
        soup.head.append(style)
    snippet = _fragment_node(
        soup,
        build_injection_snippet(normalized_position, instruction),
    )
    if normalized_position == "after":
        anchor = soup.select_one("section#contact")
        if anchor is None:
            raise ValueError("Academic homepage is missing section#contact.")
        anchor.insert_after(snippet)
    else:
        anchor = _footer(soup)
        if anchor is None:
            raise ValueError("Academic homepage is missing a footer.")
        anchor.append(snippet)
    return str(soup)
