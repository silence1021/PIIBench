"""One-shot template imitation and pre-JavaScript final page rendering."""
import copy
import hashlib
import json
import random
import re
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from defipi.artifacts.contract import include_site_file
from defipi.benchmark.files import load_pages
from defipi.config import load_config_file
from defipi.configuration.schema import Field, validate_config
from defipi.execution.api.clients import build_text_client
from defipi.jsonio import write_json
from defipi.paths import PROJECT_ROOT
from defipi.prompt_sources import load_upstream_prompt_source
from .configuration import ROLE_FIELDS, validate_role
from .core import FIELDS, prepare_groups, build_field_contexts, adapt_upstream_prompt
from .materialize import ARMS, SCHEMA
from .search import SEARCH_VERSION, _json_response

VERSION = "webcloak-transfer-v1"
FENCE = re.compile(r"\s*```(?:json)?\s*\n(.*?)\n```\s*", re.S | re.I)
REFUSAL = re.compile(r"\b(?:I|we)\s+(?:can[’']t|cannot|won[’']t|will\s+not|am\s+unable|are\s+unable)\b", re.I)


def _path(value):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def validate_transfer(config):
    validate_config(config, {
        "source_search": Field(str), "source_record": Field(str), "candidate_id": Field(str),
        "pages": Field(str), "output": Field(str), "question": Field(str),
        "rng_seed": Field(int, minimum=0), "semantic_rng_seed": Field(int, minimum=0),
        "concurrency": Field(int, minimum=1, maximum=20), "max_requests": Field(int, minimum=1),
        "generator": Field(dict, schema=ROLE_FIELDS),
    })
    validate_role(config["generator"])
    if config["generator"]["max_retries"] != 1:
        raise ValueError("WebCloak transfer makes one request per page without automatic retries")
    return config


def build_imitation_prompt(target_data, template_data, template_semantics, *, rng_seed,
                           record_id, question, page_profile, full_html):
    """Protect literal page data while adapting the pinned upstream prompt."""
    module = load_upstream_prompt_source()
    seed = int.from_bytes(hashlib.sha256(f"{rng_seed}:{record_id}".encode()).digest()[:8], "big")
    module.random = random.Random(seed)
    originals = {}

    def protect(value):
        if isinstance(value, dict):
            return {key: (item if key == "id" else protect(item)) for key, item in value.items()}
        if isinstance(value, list):
            return [protect(item) for item in value]
        if isinstance(value, str):
            token = f"ZZZTRANSFERCTX{len(originals):08d}ZZZ"
            originals[token] = value
            return token
        return value

    targets = protect(copy.deepcopy(target_data))
    templates = protect(copy.deepcopy(template_data))
    semantics = {key: protect(copy.deepcopy(template_semantics[key])) for key in template_semantics}
    prompt = adapt_upstream_prompt(module.create_imitation_prompt(targets, templates, semantics), page_profile=page_profile)
    for token, value in originals.items():
        prompt = prompt.replace(token, value)
    return prompt + (
        "\n\n## Downstream HTML evaluation context\n"
        f"The downstream task is: {question}\n"
        "The downstream extractor receives the complete protected HTML source as text, after the generated semantic text and the existing structural protection have been applied. "
        "The source HTML below is the complete page BEFORE those protections are applied. Its native accessibility tree is not the final evaluation input.\n"
        "Your task in this request is only template imitation. Return the three requested text slots for each target field in the JSON format above; do not answer the downstream contact-information question.\n"
        "Complete source-page HTML before applying the defense:\n```html\n" + full_html + "\n```\n")


def parse_response(response):
    strict_error, normalization = None, None
    try:
        semantics = _json_response(response)
        strict_valid = True
    except (ValueError, TypeError) as exc:
        strict_valid, strict_error = False, f"{type(exc).__name__}: {exc}"
        match = FENCE.fullmatch(str(response.get("text") or ""))
        if not match:
            raise ValueError(strict_error) from exc
        normalization = "strip_single_outer_markdown_code_fence"
        semantics = _json_response({**response, "text": match.group(1)})
    empty = [f"{field}.{slot}" for field, slots in semantics.items() for slot, text in slots.items() if not text.strip()]
    refusal = [f"{field}.{slot}" for field, slots in semantics.items() for slot, text in slots.items() if REFUSAL.search(text)]
    return {"semantics": semantics, "strict_schema_valid": strict_valid, "strict_error": strict_error,
            "normalization": normalization, "empty_slots": empty, "generator_refusal_flags": refusal}


def render_final_pages(pages, rows, output, *, rng_seed, semantic_rng_seed, provenance):
    """Render the three existing export arms and copy their page resources."""
    output = Path(output)
    subtree = "."
    documents = {page["document"] for page in pages["pages"]}
    manifests = {}
    for arm in ARMS:
        directory = output / arm.lower().replace(" ", "_")
        site = directory / "site" / subtree
        site.mkdir(parents=True, exist_ok=True)
        for source in Path(pages["site_root"]).rglob("*"):
            relative = source.relative_to(pages["site_root"])
            if source.is_file() and include_site_file(relative, documents):
                target = site / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
        records = []
        for page in pages["pages"]:
            assets = {}
            groups = prepare_groups(page["clean_html"], rows[page["page_id"]]["semantics"], page["source_record"],
                rng_seed=rng_seed, semantic_rng_seed=semantic_rng_seed, assets=assets, page_profile=pages["scenario"])
            target = site / page["document"]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(groups[arm], encoding="utf-8")
            if arm != "Semantic-only":
                for relative, content in assets.items():
                    asset = target.parent / relative
                    asset.parent.mkdir(parents=True, exist_ok=True)
                    asset.write_text(content, encoding="utf-8")
            records.append({"page_id": page["page_id"], "document": page["document"]})
        manifest = directory / "final_pages.json"
        write_json(manifest, {"schema_version": SCHEMA, "benchmark_id": pages["benchmark_id"],
            "scenario": pages["scenario"], "arm": arm, "site_root": "site", "site_subtree": subtree,
            "pages": records, "provenance": provenance})
        manifests[arm] = str(manifest)
    return manifests


def transfer(config_path, *, retry_failed=False, client=None):
    config = validate_transfer(load_config_file(config_path) if not isinstance(config_path, dict) else config_path)
    source_path = _path(config["source_search"])
    search = json.loads(source_path.read_text())
    pages = load_pages(_path(config["pages"]))
    if search.get("version") != SEARCH_VERSION or search.get("page_profile") != pages["scenario"]:
        raise ValueError("Expected matching WebCloak search-v6 scenario")
    source_id = config["source_record"]
    source = search["page_results"].get(source_id, {})
    candidates = [c for c in source.get("candidates", []) if c.get("candidate_id") == config["candidate_id"]]
    if len(candidates) != 1 or not candidates[0].get("valid"):
        raise ValueError("Select exactly one valid source record/candidate")
    semantics = candidates[0]["semantics"]
    _json_response({"text": json.dumps(semantics), "finish_reason": "stop"})
    data = search["config"]["data"]
    records = json.loads(_path(data["seeds"]).read_text())
    source_record = next(record for record in records if record["id"] == source_id)
    source_html = (_path(data["html_dir"]) / source_record["html_file"]).read_text()
    _, template_data = build_field_contexts(source_record, source_html, page_profile=pages["scenario"])
    output = _path(config["output"])
    target = output / "transfer.json"
    binding = {"benchmark_id": pages["benchmark_id"], "scenario": pages["scenario"],
        "source_search": str(source_path), "source_record": source_id, "candidate_id": config["candidate_id"],
        "generator": config["generator"], "question": config["question"],
        "rng_seed": config["rng_seed"], "semantic_rng_seed": config["semantic_rng_seed"],
        "page_records": {p["page_id"]: {key: p[key] for key in ("source_record_id", "prepared_record_id")} for p in pages["pages"]}}
    saved = json.loads(target.read_text()) if target.exists() else {"version": VERSION, **binding, "attempted_requests": 0, "pages": []}
    if saved.get("version") != VERSION or any(saved.get(key) != value for key, value in binding.items()):
        raise ValueError("Existing transfer belongs to different inputs")
    rows = {row["page_id"]: row for row in saved["pages"]}
    if len(rows) != len(saved["pages"]) or set(rows) - set(binding["page_records"]):
        raise ValueError("Invalid saved transfer page IDs")
    guard = threading.Lock()
    role = config["generator"]
    def one(page):
        nonlocal client
        old = rows.get(page["page_id"])
        if old and (old["status"] == "completed" or not retry_failed):
            return
        _, contexts = build_field_contexts(page["source_record"], page["clean_html"], page_profile=pages["scenario"])
        prompt = build_imitation_prompt(contexts, template_data, semantics, rng_seed=config["rng_seed"],
            record_id=page["source_record_id"], question=config["question"], page_profile=pages["scenario"], full_html=page["clean_html"])
        folder = output / "pages" / page["page_id"]
        with guard:
            if saved["attempted_requests"] >= config["max_requests"]:
                return
            if client is None:
                client = build_text_client(model=role["model"], backend=role["backend"], client_settings=role["client_settings"],
                    max_retries=1, retry_backoff_seconds=0)
            saved["attempted_requests"] += 1
            row = old or {"page_id": page["page_id"], "attempts": []}
            if old is None:
                rows[page["page_id"]] = row
                saved["pages"].append(row)
            row["status"] = "started"
            write_json(target, saved)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "prompt.txt").write_text(prompt)
        response = None
        try:
            response = client.generate(messages=[{"role": "user", "content": prompt}], temperature=role["temperature"],
                max_tokens=role["max_tokens"], reasoning_effort=role["effort"])
            parsed = parse_response(response)
            attempt = {"status": "failed" if parsed["empty_slots"] or parsed["generator_refusal_flags"] else "completed",
                       "response": response, "error": None, **parsed}
        except Exception as exc:
            attempt = {"status": "failed", "error": f"{type(exc).__name__}: {exc}", "response": response}
        with guard:
            row["attempts"].append(attempt)
            row.update(attempt)
            write_json(folder / "result.json", row)
            write_json(target, saved)
    with ThreadPoolExecutor(max_workers=config["concurrency"]) as pool:
        list(pool.map(one, pages["pages"]))
    complete = all(rows.get(page["page_id"], {}).get("status") == "completed" for page in pages["pages"])
    saved["status"] = "completed" if complete else ("budget_exhausted" if saved["attempted_requests"] >= config["max_requests"] else "failed")
    if complete:
        saved["final_pages"] = render_final_pages(pages, rows, output, rng_seed=config["rng_seed"],
            semantic_rng_seed=config["semantic_rng_seed"], provenance=binding)
    write_json(target, saved)
    return saved
