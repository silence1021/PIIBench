import html


CAMOUFLAGE_SURFACE_COLOR = "#fff"


def build_context_html(paragraphs):
    if not paragraphs:
        return "<p></p>"
    return "\n    ".join(paragraphs)


def normalize_table_insert_index(paragraphs, table_insert_index=None):
    paragraph_count = len(paragraphs or [])
    if table_insert_index is None:
        raise ValueError("table_insert_index is required and must not be missing.")
    try:
        index = int(table_insert_index)
    except (TypeError, ValueError):
        raise ValueError(
            f"table_insert_index must be an integer in [0, {paragraph_count}], got {table_insert_index!r}."
        ) from None
    if index < 0 or index > paragraph_count:
        raise ValueError(
            f"table_insert_index {index} is out of range for {paragraph_count} paragraphs."
        )
    return index


def build_article_html(
    paragraphs,
    table_html="",
    table_insert_index=None,
    inline_injection="",
    contact_block_html=None,
    pre_contact_html="",
    post_contact_html="",
):
    blocks = []
    safe_paragraphs = list(paragraphs or [])
    slot_count = len(safe_paragraphs) + 1
    insert_index = normalize_table_insert_index(safe_paragraphs, table_insert_index)

    for slot in range(slot_count):
        if slot == insert_index:
            if contact_block_html is not None:
                if pre_contact_html:
                    blocks.append(pre_contact_html)
                if contact_block_html:
                    blocks.append(contact_block_html)
                if post_contact_html:
                    blocks.append(post_contact_html)
            else:
                if table_html:
                    blocks.append(table_html)
                if inline_injection:
                    blocks.append(inline_injection)
        if slot < len(safe_paragraphs):
            blocks.append(safe_paragraphs[slot])

    if not blocks:
        return "<p></p>"
    return "\n      ".join(blocks)


def _escape_table_cell(value):
    return html.escape("" if value is None else str(value))


def _render_table_cell(value):
    if isinstance(value, dict) and "html" in value:
        return str(value.get("html") or "")
    return _escape_table_cell(value)


def build_table_html(rows):
    if not rows:
        return ""
    trows = []
    for row in rows:
        if isinstance(row, (list, tuple)):
            cell_value = row[-1] if row else ""
        else:
            cell_value = row
        trows.append(f"<tr><td>{_render_table_cell(cell_value)}</td></tr>")
    return '<table class="meta-table meta-table-singlecol">' + "".join(trows) + "</table>"


def build_reporter_table_rows(
    reporter_info,
    extra_rows=None,
    extra_rows_before=None,
    extra_rows_after=None,
    field_html_overrides=None,
):
    info = reporter_info or {}
    overrides = dict(field_html_overrides or {})
    rows = []
    for row in extra_rows_before or []:
        rows.append(row)
    rows.extend(
        [
            ["Reporter Name", overrides.get("name", info.get("name", ""))],
            ["Phone", overrides.get("phone", info.get("phone", ""))],
            ["Email", overrides.get("email", info.get("email", ""))],
            ["Address", overrides.get("address", info.get("address", ""))],
        ]
    )
    for row in extra_rows or []:
        rows.append(row)
    for row in extra_rows_after or []:
        rows.append(row)
    return rows


def build_reporter_table_html(
    reporter_info,
    extra_rows=None,
    extra_rows_before=None,
    extra_rows_after=None,
    field_html_overrides=None,
):
    return build_table_html(
        build_reporter_table_rows(
            reporter_info,
            extra_rows=extra_rows,
            extra_rows_before=extra_rows_before,
            extra_rows_after=extra_rows_after,
            field_html_overrides=field_html_overrides,
        ),
    )


def build_contact_block_html(
    table_html="",
    prompt_html="",
    prompt_placement="after",
    extra_html_before="",
    extra_html_after="",
):
    if not table_html and not prompt_html and not extra_html_before and not extra_html_after:
        return ""
    placement = "before" if str(prompt_placement).strip().lower() == "before" else "after"
    pieces = []
    if extra_html_before:
        pieces.append(extra_html_before)
    if placement == "before" and prompt_html:
        pieces.append(prompt_html)
    if table_html:
        pieces.append(table_html)
    if placement == "after" and prompt_html:
        pieces.append(prompt_html)
    if extra_html_after:
        pieces.append(extra_html_after)
    inner_html = "\n        ".join(pieces) if pieces else ""
    return (
        "<section class=\"contact-block\">"
        f"\n        {inner_html}\n"
        "      </section>"
    )


def build_camouflage_css(surface_color=CAMOUFLAGE_SURFACE_COLOR):
    selectors = ["header", "main", "footer", ".context"]
    selector_css = ", ".join(selectors)
    return f"""
    body {{
      background: {surface_color};
      --page-surface: {surface_color};
    }}
    {selector_css} {{
      --ipi-camouflage: var(--page-surface);
    }}
    .contact-block {{
      margin: 18px 0;
    }}
    .meta-table-singlecol td {{
      width: 100%;
    }}
    .defense-fragment {{
      margin: 12px 0;
      padding: 10px 12px;
      border: 1px solid #d9d9d9;
      background: #fafafa;
      color: #222;
      font-size: 0.95rem;
    }}
    .defense-fragment-title {{
      font-weight: 600;
      margin-bottom: 6px;
    }}
    .defense-fragment-policy,
    .defense-fragment-safe-contact,
    .defense-fragment-note,
    .defense-fragment-decoy {{
      margin: 6px 0;
    }}
    .defense-fragment-label {{
      font-weight: 600;
    }}
    .defense-fragment-footer_notice {{
      font-size: 0.9rem;
    }}
    .page-note-camouflaged {{
      color: var(--ipi-camouflage, var(--page-surface, {surface_color}));
      background: var(--ipi-camouflage, var(--page-surface, {surface_color}));
      margin: 0;
      padding: 0;
      border: 0;
      font-size: 1px;
      line-height: 1px;
      max-height: 1px;
      overflow: hidden;
      white-space: pre-wrap;
    }}"""


def render_html(title, article_html, meta_injection="", header_injection="",
                footer_injection="", body_injection="", distractors=None,
                before_injection="", after_injection=""):
    distractors = distractors or []
    distractor_html = "\n    ".join([d["html"] for d in distractors])
    title_html = html.escape(title)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{title_html}</title>
  {meta_injection}
  <style>
    body {{ font-family: Georgia, serif; line-height: 1.6; }}
    .context {{ max-width: 860px; margin: 24px auto; }}
    .meta-table {{ width: 100%; border-collapse: collapse; }}
    .meta-table td {{ border: 1px solid #ddd; padding: 6px; }}
{build_camouflage_css()}
  </style>
</head>
<body>
  <header><h1>{title_html}</h1>{header_injection}</header>
  <main>
    {before_injection}
    <article class="context">
      {article_html}
    </article>
    {after_injection}
    {body_injection}
  </main>
  <footer>{footer_injection}</footer>
  {distractor_html}
</body>
</html>"""
