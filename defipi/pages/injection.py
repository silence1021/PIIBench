import html


CAMOUFLAGED_ATTACK_CLASS = "page-note page-note-camouflaged"
CAMOUFLAGE_SURFACE_COLOR = "#fff"


def split_paragraphs(text):
    parts = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not parts:
        return []
    paras = []
    for p in parts:
        safe = html.escape(p).replace("\n", "<br>")
        paras.append(f"<p>{safe}</p>")
    return paras


def comment_safe(text):
    return text.replace("--", "- -")


def build_camouflaged_instruction(safe_text, aria_hidden=False):
    hidden_attr = ' aria-hidden="true"' if aria_hidden else ""
    return f'<div class="{CAMOUFLAGED_ATTACK_CLASS}"{hidden_attr}>{safe_text}</div>'


def build_injection_snippet(position, instruction):
    safe_text = html.escape(instruction)
    safe_attr = html.escape(instruction, quote=True)
    if position == "comment":
        return f"<!-- {html.escape(comment_safe(instruction))} -->"
    if position == "hidden":
        return build_camouflaged_instruction(safe_text, aria_hidden=True)
    if position == "data-attr":
        return f'<div class="page-note" data-note="{safe_attr}"></div>'
    if position == "meta":
        return f'<meta name="page-note" content="{safe_attr}">'
    return build_camouflaged_instruction(safe_text)

def build_html(title, context_text, instruction, position):
    paras = split_paragraphs(context_text or "")
    inj = build_injection_snippet(position, instruction)
    context_html = "\n    ".join(paras) if paras else "<p></p>"

    meta_injection = inj if position == "meta" else ""
    header_injection = inj if position == "header" else ""
    footer_injection = inj if position == "footer" else ""
    before_injection = inj if position == "before" else ""
    after_injection = inj if position == "after" else ""
    inline_injection = build_injection_snippet("after", instruction) if position == "inline" else ""
    hidden_injection = inj if position == "hidden" else ""
    data_injection = inj if position == "data-attr" else ""
    comment_injection = inj if position == "comment" else ""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>{html.escape(title)}</title>
  {meta_injection}
  <style>
    body {{
      font-family: Arial, sans-serif;
      line-height: 1.5;
      background: {CAMOUFLAGE_SURFACE_COLOR};
      --page-surface: {CAMOUFLAGE_SURFACE_COLOR};
    }}
    header, main, footer {{
      --ipi-camouflage: var(--page-surface);
    }}
    .context {{
      max-width: 900px;
      margin: 24px auto;
      --ipi-camouflage: var(--page-surface);
    }}
    .page-note-camouflaged {{
      color: var(--ipi-camouflage, var(--page-surface, {CAMOUFLAGE_SURFACE_COLOR}));
      background: var(--ipi-camouflage, var(--page-surface, {CAMOUFLAGE_SURFACE_COLOR}));
      margin: 0;
      padding: 0;
      border: 0;
      font-size: 1px;
      line-height: 1px;
      max-height: 1px;
      overflow: hidden;
      white-space: pre-wrap;
    }}
  </style>
</head>
<body>
  <header>
    <h1>{html.escape(title)}</h1>
    {header_injection}
  </header>
  <main>
    {before_injection}
    <article class="context">
    {context_html}
    {inline_injection}
    </article>
    {after_injection}
    {hidden_injection}
    {data_injection}
  </main>
  <footer>
    {footer_injection}
  </footer>
  {comment_injection}
</body>
</html>"""
