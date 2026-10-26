"""Prepare portable benchmark sites, page inputs and frozen tasks offline."""
from collections import Counter
import json
from pathlib import Path
import shutil
import tempfile
from bs4 import BeautifulSoup
from defipi.artifacts.contract import include_site_file, safe_relative, unique
from defipi.configuration.schema import Field, Problem, ValidationError, validate_config
from .files import PAGES_SCHEMA_VERSION, TASKS_SCHEMA_VERSION, load_pages, load_tasks
from defipi.pages.reporter_info import resolve_clean_reporter_info
from defipi.academic_pages.layout import require_academic_sidebar_layout

FIELDS = ("name", "email", "phone", "address")
RECORD_FIELDS = {
    "scenario": Field(str, choices=("academic", "news")), "page_id": Field(str), "case_id": Field(str),
    "task_type": Field(str, choices=("pii", "benign_qa")), "html_file": Field(str), "resource_root": Field(str),
    "question": Field(str), "metadata": Field(dict),
    # Task truth: PII tasks supply reporter_info, benign QA tasks supply ideal.
    "reporter_info": Field(dict, required=False), "ideal": Field((str, list), required=False),
    # Page bindings used by URL export and GEPA/PIIGuard rendering.
    "url_path": Field(str, required=False), "source_record": Field(dict, required=False),
    "source_record_id": Field(str, required=False), "prepared_record_id": Field(str, required=False),
}
def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))
def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
def validate_html(text, truth, scenario, label):
    if scenario == "academic":
        require_academic_sidebar_layout(text)
    visible = " ".join(BeautifulSoup(text, "html.parser").get_text(" ", strip=True).split())
    # Preserve the existing matching semantics, without importing evaluation.
    from defipi.evaluation.field_matching import field_in_text
    missing = [f for f in FIELDS if not field_in_text(f, visible, truth.get(f))]
    if missing:
        raise ValueError(f"Missing complete contact fields in {label}: {missing}")

def _write_benchmark(output, benchmark_id, groups):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError(f"Benchmark output already exists: {output}")
    summaries = {}
    for scenario, records in groups.items():
        if scenario not in ("academic", "news"):
            raise ValueError(f"Unsupported scenario: {scenario}")
        directory = output / scenario
        site = directory / "site"
        bindings = {}
        documents_by_root = {}
        for p in records:
            binding = {k: v for k, v in p.items() if k != "task"}
            if p["page_id"] in bindings and bindings[p["page_id"]] != binding:
                raise ValueError(f"Conflicting page binding: {p['page_id']}")
            bindings[p["page_id"]] = binding
            documents_by_root.setdefault(Path(p["resource_root"]).resolve(), set()).add(
                safe_relative(p["document"], "document"))
        copied = set()
        occupied = {}
        pages, tasks = [], []
        for record in records:
            source_root = Path(record["resource_root"]).resolve()
            if output.is_relative_to(source_root):
                raise ValueError("Benchmark output must be outside source site")
            copy_root = Path(record["copy_root"]).resolve()
            if not copy_root.is_dir() or not copy_root.is_relative_to(source_root):
                raise ValueError("copy_root must be a directory inside resource_root")
            document = safe_relative(record["document"], "document")
            if not (source_root / document).resolve().is_relative_to(copy_root):
                raise ValueError("document must be inside copy_root")
            copy_binding = (source_root, copy_root)
            if copy_binding not in copied:
                for source in copy_root.rglob("*"):
                    if not source.is_file():
                        continue
                    relative = source.relative_to(source_root)
                    if not include_site_file(relative, documents_by_root[source_root]):
                        continue
                    if relative in occupied and occupied[relative] != source:
                        raise ValueError(f"Conflicting source site path: {relative}")
                    occupied[relative] = source
                    target = site / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                copied.add(copy_binding)
            if not any(p["page_id"] == record["page_id"] for p in pages):
                pages.append({k: v for k, v in record.items() if k in ("page_id", "document", "url_path", "source_record_id", "prepared_record_id", "source_record")})
            tasks.append(record["task"])
        common = {"benchmark_id": benchmark_id, "scenario": scenario}
        write(directory / "pages.json", {"schema_version": PAGES_SCHEMA_VERSION, **common, "site_root": "site", "pages": pages})
        write(directory / "tasks.json", {"schema_version": TASKS_SCHEMA_VERSION, **common, "tasks": tasks})
        load_pages(directory / "pages.json")
        load_tasks(directory / "tasks.json")
        summaries[scenario] = {"pages": len(pages), "question_counts": dict(Counter(t["metadata"]["question_id"] for t in tasks)), "pages_file": str(directory / "pages.json"), "tasks_file": str(directory / "tasks.json")}
    return {"benchmark_id": benchmark_id, "scenarios": summaries}

def write_benchmark(output, benchmark_id, groups):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError(f"Benchmark output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".benchmark-", dir=output.parent) as temporary:
        stage = Path(temporary) / "prepared"
        result = _write_benchmark(stage, benchmark_id, groups)
        stage.rename(output)
    for scenario, summary in result["scenarios"].items():
        summary["pages_file"] = str(output / scenario / "pages.json")
        summary["tasks_file"] = str(output / scenario / "tasks.json")
    return result

def prepare_suite(suite_path, output, *, root):
    root = Path(root).resolve()
    suite_path = Path(suite_path).resolve()
    suite = read(suite_path)
    rows = [json.loads(line) for line in (root / suite["dataset"]["source"]).read_text(encoding="utf-8").splitlines() if line.strip()]
    assignments_list = read(suite_path.parent / suite["dataset"]["question_assignments"])["pages"]
    unique([a["page_id"] for a in assignments_list], "page_id")
    assignments = {a["page_id"]: a for a in assignments_list}
    unique([p["page_id"] for p in rows], "page_id")
    if {p["page_id"] for p in rows} != set(assignments):
        raise ValueError("Configured final pages and question assignments differ")
    if Counter(p["domain"] for p in rows) != suite["dataset"]["scenarios"]:
        raise ValueError("Final page counts differ from suite")
    groups, cache = {}, {}
    for row in rows:
        scenario = row["domain"]
        a = assignments[row["page_id"]]
        if a["scenario"] != scenario or a["base_request_id"] != "B1":
            raise ValueError(f"Unexpected question assignment: {row['page_id']}")
        source_file = root / row["source_seeds"]
        if source_file not in cache:
            cache[source_file] = {p["id"]: p for p in read(source_file)}
        seed = cache[source_file][row["prepared_record_id"]]
        truth = {"name": row["target_identity_label"], **row["protected_fields"]}
        if truth != resolve_clean_reporter_info(seed):
            raise ValueError(f"Final page truth differs from source: {row['page_id']}")
        html = (root / row["source_html"]).resolve()
        validate_html(html.read_text(encoding="utf-8"), truth, scenario, row["page_id"])
        source_root = html.parents[2] if scenario == "academic" else html.parent
        metadata = {"scenario": scenario, "question_id": a["prompt_id"], **{k: a[k] for k in ("prompt_scenario_id", "base_request_id", "version")}, **{k: row.get(k) for k in ("source_record_id", "prepared_record_id", "split")}}
        record = {"page_id": row["page_id"], "resource_root": str(source_root), "copy_root": str(html.parent if scenario == "academic" else source_root), "document": html.relative_to(source_root).as_posix(), "source_record_id": row["source_record_id"], "prepared_record_id": row["prepared_record_id"], "source_record": {k: v for k, v in seed.items() if k != "template_style"}, "task": {"case_id": row["page_id"] + "-b1", "page_id": row["page_id"], "task_type": "pii", "question": a["question"], "reporter_info": truth, "metadata": metadata}}
        groups.setdefault(scenario, []).append(record)
    for scenario, records in groups.items():
        counts = Counter(p["task"]["metadata"]["question_id"] for p in records)
        if len(set(counts.values())) != 1:
            raise ValueError(f"Unbalanced question assignment: {scenario}")
    return write_benchmark(output, suite["suite_id"], groups)

def prepare_records(records_path, output, *, benchmark_id):
    records_path = Path(records_path).resolve()
    records = read(records_path)
    if not isinstance(records, list) or not records:
        raise ValueError("Records must be a non-empty JSON array")
    problems = []
    for index, row in enumerate(records):
        prefix = f"records[{index}]"
        try:
            validate_config(row, RECORD_FIELDS, prefix=prefix)
        except ValidationError as exc:
            problems.extend(exc.problems)
        metadata = row.get("metadata") if type(row) is dict else None
        if type(metadata) is dict and (type(metadata.get("question_id")) is not str or not metadata["question_id"].strip()):
            problems.append(Problem(f"{prefix}.metadata.question_id", "required nonempty string"))
    if problems:
        raise ValidationError(problems)
    groups = {}
    for row in records:
        scenario = row["scenario"]
        html = (records_path.parent / row["html_file"]).resolve()
        root = (records_path.parent / row["resource_root"]).resolve()
        if not html.is_relative_to(root):
            raise ValueError("html_file must be inside resource_root")
        record = {k: row[k] for k in ("page_id", "url_path", "source_record", "source_record_id", "prepared_record_id") if k in row}
        if "source_record" in record:
            record["source_record"] = {k: v for k, v in record["source_record"].items() if k != "template_style"}
        record.update(resource_root=str(root), copy_root=str(root), document=html.relative_to(root).as_posix())
        record["task"] = {"case_id": row["case_id"], "page_id": row["page_id"], "task_type": row["task_type"], "question": row["question"], "metadata": row["metadata"]}
        for key in ("reporter_info", "ideal"):
            if key in row:
                record["task"][key] = row[key]
        groups.setdefault(scenario, []).append(record)
    return write_benchmark(output, benchmark_id, groups)
