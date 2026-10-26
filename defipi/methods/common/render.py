"""Render News pages and selected defense fragments."""
from defipi.methods.common.defense_fragments import (
    build_fragment_table_rows,
    normalize_fragment_spec,
    render_defense_fragment,
)
from defipi.pages.html_templates import (
    build_article_html,
    build_contact_block_html,
    build_reporter_table_html,
    render_html,
)
from defipi.pages.injection import build_injection_snippet, split_paragraphs
from defipi.pages.reporter_info import (
    resolve_rendered_reporter_info,
)


def render_seed_html(seed_record):
    context = seed_record.get("context", "")
    reporter_info = resolve_rendered_reporter_info(seed_record)
    pos = seed_record.get("injection_position") or "after"
    instruction = seed_record.get("attack_instruction") or ""
    fragment_spec = seed_record.get("defense_fragment")
    fragment = None
    fragment_html = ""
    fragment_table_rows = []
    if fragment_spec is not None:
        fragment = normalize_fragment_spec(fragment_spec)
        pos = fragment["slot"]
        fragment_html = render_defense_fragment(fragment)
        fragment_table_rows = build_fragment_table_rows(fragment)
        instruction = ""
    table_insert_index = seed_record.get("table_insert_index")
    if table_insert_index is None:
        seed_label = seed_record.get("slug") or seed_record.get("id") or "unknown-seed"
        raise ValueError(f"Seed '{seed_label}' is missing required table_insert_index.")

    paragraphs = split_paragraphs(context)
    table_html = build_reporter_table_html(
        reporter_info,
        extra_rows=fragment_table_rows,
    )

    bio_tail_html = fragment_html if fragment and fragment["slot"] == "bio_tail" else ""
    contact_fragment_html = (
        fragment_html
        if fragment and fragment["slot"] == "contact_block" and not fragment_table_rows
        else ""
    )
    footer_fragment_html = fragment_html if fragment and fragment["slot"] == "footer_notice" else ""
    meta_injection = build_injection_snippet("meta", instruction) if pos == "meta" and instruction else ""
    header_injection = build_injection_snippet("header", instruction) if pos == "header" and instruction else ""
    if fragment and fragment["slot"] == "header_notice":
        header_injection = fragment_html
    footer_injection = build_injection_snippet("footer", instruction) if pos == "footer" and instruction else ""
    if footer_fragment_html:
        footer_injection = "\n".join(part for part in (footer_injection, footer_fragment_html) if part)
    bound_prompt_html = ""
    bound_prompt_placement = "after"
    if instruction and pos in ("before", "after", "inline"):
        bound_prompt_html = build_injection_snippet("after", instruction)
        bound_prompt_placement = "before" if pos == "before" else "after"
    contact_block_html = build_contact_block_html(
        table_html=table_html,
        prompt_html=bound_prompt_html,
        prompt_placement=bound_prompt_placement,
        extra_html_after=contact_fragment_html,
    )
    article_html = build_article_html(
        paragraphs,
        table_insert_index=table_insert_index,
        contact_block_html=contact_block_html,
        pre_contact_html=bio_tail_html,
    )
    body_injection = (
        build_injection_snippet(pos, instruction)
        if instruction and pos not in ("meta", "header", "footer", "inline", "before", "after")
        else ""
    )

    rendered_html = render_html(
        seed_record.get("id") or "seed",
        article_html,
        meta_injection=meta_injection,
        header_injection=header_injection,
        footer_injection=footer_injection,
        body_injection=body_injection,
        distractors=[],
    )
    if fragment and fragment.get("format") == "ax_edit_v1":
        from defipi.methods.common.ax_edits import compile_news_ax_edits
        rendered_html, _realization = compile_news_ax_edits(rendered_html, fragment)
    return rendered_html


def render_fragment_page(page, candidate, *, scenario):
    """Render a selected fragment with the same page renderer used in search."""
    if scenario == "academic":
        from defipi.methods.common.academic_inject import inject_academic_defense
        return inject_academic_defense(page["clean_html"], position=candidate["position"],
            attack_instruction=candidate.get("attack_instruction", ""),
            defense_fragment=candidate["defense_fragment"])
    if scenario != "news":
        raise ValueError(f"Unsupported scenario: {scenario}")
    if not page.get("source_record"):
        raise ValueError("News fragment export requires source_record")
    return render_seed_html({**page["source_record"], "injection_position": candidate["position"],
        "defense_fragment": candidate["defense_fragment"]})
