"""Benchmark page inputs and independent evaluation tasks."""
import copy
from pathlib import Path
from defipi.artifacts.contract import ArtifactError, read_json, resolve_from, safe_relative, unique, declared_resources
PAGES_SCHEMA_VERSION = "piibench-pages-v1"
TASKS_SCHEMA_VERSION = "piibench-tasks-v1"

def normalise_tasks(tasks, page_ids):
    if not isinstance(tasks, list) or not tasks:
        raise ArtifactError("Benchmark tasks must be a non-empty list")
    result = []
    case_ids = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ArtifactError(f"tasks[{index}] must be an object")
        case_id = str(task.get("case_id") or "").strip()
        page_id = str(task.get("page_id") or "").strip()
        task_type = str(task.get("task_type") or "").strip().lower()
        question = task.get("question")
        if not case_id or not page_id:
            raise ArtifactError(f"tasks[{index}] requires case_id and page_id")
        if page_id not in page_ids:
            raise ArtifactError(f"Task {case_id} references unknown page_id: {page_id}")
        if task_type not in ("pii", "benign_qa"):
            raise ArtifactError(f"Task {case_id} has unsupported task_type: {task_type}")
        if not isinstance(question, str) or not question.strip():
            raise ArtifactError(f"Task {case_id} requires a non-empty question")
        item = {"case_id": case_id, "page_id": page_id, "task_type": task_type,
                "question": question}
        metadata = task.get("metadata")
        if not isinstance(metadata, dict):
            raise ArtifactError(f"Task {case_id} requires a metadata object")
        if not isinstance(metadata.get("question_id"), str) or not metadata["question_id"].strip():
            raise ArtifactError(f"Task {case_id} requires a non-empty string metadata.question_id")
        item["metadata"] = copy.deepcopy(metadata)
        if task_type == "pii":
            reporter = task.get("reporter_info")
            if not isinstance(reporter, dict) or any(
                not isinstance(reporter.get(field), str) or not reporter[field].strip()
                for field in ("name", "email", "phone", "address")
            ):
                raise ArtifactError(f"PII task {case_id} requires complete reporter_info")
            item["reporter_info"] = {field: reporter[field] for field in
                                     ("name", "email", "phone", "address")}
        else:
            aliases = task.get("ideal")
            if isinstance(aliases, str):
                aliases = [aliases]
            if not isinstance(aliases, list) or not any(
                isinstance(value, str) and value.strip() for value in aliases
            ):
                raise ArtifactError(f"Benign QA task {case_id} requires non-empty ideal aliases")
            item["ideal"] = [value for value in aliases if isinstance(value, str) and value.strip()]
        result.append(item)
        case_ids.append(case_id)
    unique(case_ids, "case_id")
    return result



def load_tasks(path):
    data = read_json(path)
    if data.get("schema_version") != TASKS_SCHEMA_VERSION:
        raise ArtifactError(f"tasks schema must be {TASKS_SCHEMA_VERSION}")
    if not isinstance(data.get("benchmark_id"), str) or not data["benchmark_id"].strip():
        raise ArtifactError("benchmark_id is required")
    if data.get("scenario") not in ("academic", "news"):
        raise ArtifactError("scenario must be academic or news")
    tasks = data.get("tasks")
    return {**data, "tasks": normalise_tasks(tasks, {t.get("page_id") for t in tasks or []})}

def load_pages(path):
    path = Path(path).resolve()
    data = read_json(path)
    if data.get("schema_version") != PAGES_SCHEMA_VERSION:
        raise ArtifactError(f"pages schema must be {PAGES_SCHEMA_VERSION}")
    if not isinstance(data.get("benchmark_id"), str) or not data["benchmark_id"].strip():
        raise ArtifactError("benchmark_id is required")
    if data.get("scenario") not in ("academic", "news"):
        raise ArtifactError("scenario must be academic or news")
    root = resolve_from(path.parent, data.get("site_root"), "site_root")
    pages = []
    for item in data.get("pages", []):
        doc = safe_relative(item.get("document"), "document")
        html = (root / doc).resolve()
        if not html.is_relative_to(root) or not html.is_file():
            raise ArtifactError(f"Missing page: {html}")
        text = html.read_text(encoding="utf-8")
        declared_resources(text, root, doc)
        pages.append({**item, "clean_html": text, "html_path": str(html), "resource_root": str(root), "resource_document_path": doc})
    if not pages:
        raise ArtifactError("Benchmark requires pages")
    unique([p["page_id"] for p in pages], "page_id")
    return {**data, "site_root": str(root), "pages": pages}
