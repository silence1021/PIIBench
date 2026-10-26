"""Bind already generated final WebCloak pages without search or inference."""
import argparse
import json
from pathlib import Path
import shutil
from defipi.artifacts.contract import include_site_file, safe_relative
from defipi.benchmark.files import load_pages

SCHEMA = "webcloak-final-pages-v1"
ARMS = ("Structure-only", "Semantic-only", "Full WebCloak-PII")
MAPPING_FIELDS = {"page_id", "document", "url_path"}

def materialize(pages_path, site_root, mapping_path, output, *, site_subtree, arm, metadata_path=None):
    pages = load_pages(pages_path)
    if arm not in ARMS:
        raise ValueError("Unsupported WebCloak arm")
    site_root, output = Path(site_root).resolve(), Path(output).resolve()
    site_subtree = safe_relative(site_subtree, "site_subtree")
    selected = (site_root / site_subtree).resolve()
    if selected == site_root or not selected.is_relative_to(site_root) or not selected.is_dir():
        raise ValueError("Missing selected WebCloak site subtree")
    mapping = json.loads(Path(mapping_path).read_text())
    if not isinstance(mapping, list) or not all(isinstance(row, dict) for row in mapping):
        raise ValueError("Mapping must be a list of {page_id, document} rows")
    for row in mapping:
        if set(row) - MAPPING_FIELDS:
            raise ValueError(f"Unsupported mapping fields {sorted(set(row) - MAPPING_FIELDS)}; "
                             "put existing generation records in --metadata")
    indexed = {row.get("page_id"): row for row in mapping}
    if len(indexed) != len(mapping) or set(indexed) != {p["page_id"] for p in pages["pages"]}:
        raise ValueError("Final WebCloak mapping must cover exactly the benchmark pages")
    records = []
    for page in pages["pages"]:
        row = indexed[page["page_id"]]
        document = safe_relative(row.get("document"), "document")
        path = (site_root / document).resolve()
        if not path.is_relative_to(selected) or not path.is_file():
            raise ValueError(f"Missing generated final page: {page['page_id']}: {document}")
        if row.get("url_path"):
            safe_relative(row["url_path"], "url_path")
        records.append({"page_id": page["page_id"], "document": document,
                        **({"url_path": row["url_path"]} if row.get("url_path") else {})})
    metadata = json.loads(Path(metadata_path).read_text()) if metadata_path else {}
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    if output.is_relative_to(site_root):
        raise ValueError("Output must be outside the selected source site")
    output.mkdir(parents=True)
    documents = {row["document"] for row in records}
    for source in selected.rglob("*"):
        relative = source.relative_to(site_root)
        if not source.is_file() or not include_site_file(relative, documents):
            continue
        target = output / "site" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    saved = {"schema_version": SCHEMA, "benchmark_id": pages["benchmark_id"], "scenario": pages["scenario"],
             "arm": arm, "site_root": "site", "site_subtree": site_subtree, "pages": records, "provenance": metadata}
    target = output / "final_pages.json"
    target.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n")
    return target

def add_arguments(parser):
    for name in ("pages", "site-root", "site-subtree", "mapping", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--arm", choices=ARMS, required=True)
    parser.add_argument("--metadata", help="Existing generation records (candidates, seeds, record IDs) "
                        "copied into provenance; never dereferenced")


def run_cli(args):
    print(materialize(args.pages, args.site_root, args.mapping, args.output, site_subtree=args.site_subtree, arm=args.arm, metadata_path=args.metadata))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    return run_cli(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
