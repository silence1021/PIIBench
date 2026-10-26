"""Orchestration for preparing templates and generating paired base pages."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping

from .backends import (
    BACKEND_IDS,
    ACADEMIC_PAGES_LOCK,
    TEMPLATE_LOCKS,
    AcademicTemplateBackend,
    make_backend,
)
from .curated import (
    CURATION_POLICY,
    CuratedScriptedBuilder,
    finalize_build_validation,
)
from .layout import normalize_academic_sidebar_layout
from defipi.jsonio import content_key
from ..synthetic_personas import (
    SyntheticPersona,
    SyntheticPersonaManifest,
    load_persona_manifest,
)
from defipi.jsonio import write_json


_REUSE_INDEX_FILENAME = ".persona-reuse-index.json"


def _run(command: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"Command failed ({' '.join(command)}): {detail}")
    return completed


def prepare_templates(
    cache_root: str | Path,
    *,
    prepare_docker: bool,
) -> dict[str, Any]:
    """Fetch exact upstream revisions and optionally prepare pinned build images."""

    from defipi.configuration.schema import Field, validate_config
    validate_config({"prepare_docker": prepare_docker}, {"prepare_docker": Field(bool)})
    root = Path(cache_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    prepared = []
    for backend_id in BACKEND_IDS:
        lock = TEMPLATE_LOCKS[backend_id]
        target = root / lock.cache_dirname
        downloaded = False
        if not target.exists():
            _run(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch",
                    lock.tag,
                    lock.repository,
                    str(target),
                ]
            )
            downloaded = True
        license_path = target / "LICENSE"
        if not license_path.is_file():
            raise FileNotFoundError(f"{backend_id} template is missing LICENSE")
        prepared.append(
            {
                "template_backend": backend_id,
                "path": str(target),
                "tag": lock.tag,
                "downloaded": downloaded,
            }
        )

    docker = {"prepared": False, "images": []}
    if prepare_docker:
        _run(["docker", "info", "--format", "{{.ServerVersion}}"])
        academic_root = root / ACADEMIC_PAGES_LOCK.cache_dirname
        _run(
            [
                "docker",
                "build",
                "--tag",
                ACADEMIC_PAGES_LOCK.docker_image,
                str(academic_root),
            ]
        )
        al_folio_image = TEMPLATE_LOCKS["al_folio"].docker_image
        _run(["docker", "pull", al_folio_image])
        docker = {
            "prepared": True,
            "images": [ACADEMIC_PAGES_LOCK.docker_image, al_folio_image],
        }
    return {"status": "ok", "cache_root": str(root), "templates": prepared, "docker": docker}


def _build_id(manifest):
    return "academic-" + content_key({
        "personas": [_persona_key(persona) for persona in manifest.personas],
        "templates": _template_manifest(),
        "curation_policy": CURATION_POLICY,
    })[:16]


def _template_manifest() -> dict[str, dict[str, str]]:
    return {
        backend_id: {
            "repository": lock.repository,
            "tag": lock.tag,
            "docker_image": lock.docker_image,
        }
        for backend_id, lock in TEMPLATE_LOCKS.items()
    }


def _persona_key(persona: SyntheticPersona) -> str:
    # Compare the actual build inputs when selecting an incremental cache entry.
    return content_key([persona.canonical_dict(), content_key(persona.avatar.read_bytes())])


def _validated_jobs(jobs: int) -> int:
    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs < 1:
        raise ValueError("jobs must be a positive integer")
    return jobs


def _write_reuse_index(
    build_dir: Path,
    manifest: SyntheticPersonaManifest,
) -> None:
    payload = {
        "version": 1,
        "build_id": build_dir.name,
        "persona_key": {
            persona.id: _persona_key(persona) for persona in manifest.personas
        },
    }
    path = build_dir / _REUSE_INDEX_FILENAME
    write_json(path, payload, sort_keys=True)


def _read_reuse_index(build_dir: Path) -> dict[str, str]:
    path = build_dir / _REUSE_INDEX_FILENAME
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if payload.get("build_id") != build_dir.name:
        return {}
    values = payload.get("persona_key")
    if not isinstance(values, dict):
        return {}
    return {str(key): str(value) for key, value in values.items() if key and value}


def _reusable_artifacts(output: Path) -> dict[tuple[str, str], Path]:
    reusable: dict[tuple[str, str], Path] = {}
    for build_dir in sorted(output.glob("academic-*"), reverse=True):
        manifest_path = build_dir / "generation_manifest.json"
        validation_path = build_dir / "validation.json"
        if not manifest_path.is_file() or not validation_path.is_file():
            continue
        try:
            generation = json.loads(manifest_path.read_text(encoding="utf-8"))
            validation = json.loads(validation_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if generation.get("curation_policy") != CURATION_POLICY or not validation.get("ok"):
            continue
        reuse_index = _read_reuse_index(build_dir)
        for record in generation.get("pages") or []:
            backend_id = str(record.get("template_backend") or "")
            persona_id = str(record.get("persona_id") or "")
            persona_key = str(
                record.get("persona_key") or reuse_index.get(persona_id) or ""
            )
            lock = TEMPLATE_LOCKS.get(backend_id)
            if (
                not lock
                or not persona_id
                or not persona_key
                or record.get("template_tag") != lock.tag
            ):
                continue
            key = (backend_id, persona_key)
            if key in reusable:
                continue
            candidate = build_dir / backend_id / persona_id
            if (
                not (candidate / "index.html").is_file()
                or not (candidate / "publications" / "index.html").is_file()
            ):
                continue
            reusable[key] = candidate
    return reusable


def generate_academic_pages(
    personas: str | Path | SyntheticPersonaManifest,
    *,
    output_root: str | Path,
    template_cache_root: str | Path,
    browser_validation: bool,
    backend_overrides: Mapping[str, AcademicTemplateBackend] | None = None,
    jobs: int,
    incremental: bool,
    validation_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    manifest = (
        personas
        if isinstance(personas, SyntheticPersonaManifest)
        else load_persona_manifest(personas)
    )
    backend_ids = BACKEND_IDS
    from defipi.configuration.generation import ACADEMIC_VALIDATION_FIELDS
    from defipi.configuration.schema import validate_config
    if type(browser_validation) is not bool or type(incremental) is not bool:
        raise ValueError("browser_validation and incremental must be bool")
    if browser_validation:
        validate_config(validation_settings, ACADEMIC_VALIDATION_FIELDS)
    elif validation_settings is not None:
        raise ValueError("validation_settings is inactive when browser_validation is false")
    output = Path(output_root).expanduser().resolve()
    cache_root = Path(template_cache_root).expanduser().resolve()
    requested_jobs = _validated_jobs(jobs)
    effective_jobs = 1 if backend_overrides else requested_jobs
    output.mkdir(parents=True, exist_ok=True)
    build_id = _build_id(manifest)
    final_dir = output / build_id
    if final_dir.exists():
        manifest_path = final_dir / "generation_manifest.json"
        validation_path = final_dir / "validation.json"
        if not manifest_path.is_file() or not validation_path.is_file():
            raise FileExistsError(f"Incomplete academic page build already exists: {final_dir}")
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        if not validation.get("ok"):
            raise ValueError(f"Existing academic page build is not valid: {final_dir}")
        _write_reuse_index(final_dir, manifest)
        page_count = int(existing.get("page_count") or 0)
        return {
            "status": "ok",
            "reused": True,
            "build_dir": str(final_dir),
            "manifest": existing,
            "build_stats": {
                "built_page_count": 0,
                "reused_page_count": page_count,
                "jobs": 0,
                "exact_build_reuse": True,
            },
        }

    stage = output / f".{build_id}.tmp-{uuid.uuid4().hex[:8]}"
    stage.mkdir()
    reusable = _reusable_artifacts(output) if incremental else {}
    try:
        with tempfile.TemporaryDirectory(prefix="academic-pages-work-", dir=str(output)) as temporary:
            work_root = Path(temporary)
            tasks = [
                (persona, backend_id)
                for persona in manifest.personas
                for backend_id in backend_ids
            ]

            def build_one(task: tuple[SyntheticPersona, str]) -> tuple[dict[str, Any], bool]:
                persona, backend_id = task
                persona_key = _persona_key(persona)
                destination = stage / backend_id / persona.id
                cached = reusable.get((backend_id, persona_key))
                built = cached is None
                if cached is not None:
                    shutil.copytree(cached, destination)
                else:
                    backend = (backend_overrides or {}).get(backend_id)
                    if backend is None:
                        backend = make_backend(backend_id, cache_root)
                    workspace = work_root / backend_id / persona.id
                    backend.materialize(persona, workspace)
                    source_site = backend.build(workspace)
                    CuratedScriptedBuilder().package(
                        source_site,
                        destination,
                        template_license=backend.template_root / "LICENSE",
                    )
                if backend_id == "academic_pages":
                    homepage = destination / "index.html"
                    html, _layout = normalize_academic_sidebar_layout(homepage.read_text(encoding="utf-8"))
                    homepage.write_text(html, encoding="utf-8")
                return (
                    {
                        "page_id": f"{backend_id}:{persona.id}",
                        "persona_id": persona.id,
                        "persona_key": persona_key,
                        "template_backend": backend_id,
                        "template_tag": TEMPLATE_LOCKS[backend_id].tag,
                        "variant": "curated_scripted_base",
                        "html_path": f"{backend_id}/{persona.id}/index.html",
                        "publication_html_path": (
                            f"{backend_id}/{persona.id}/publications/index.html"
                        ),
                    },
                    built,
                )

            if effective_jobs == 1:
                results = [build_one(task) for task in tasks]
            else:
                with ThreadPoolExecutor(max_workers=effective_jobs) as executor:
                    results = list(executor.map(build_one, tasks))

            records = [record for record, _built in results]
            built_count = sum(built for _record, built in results)
            reused_count = len(results) - built_count

        generation_manifest: dict[str, Any] = {
            "version": 1,
            "build_id": build_id,
            "curation_policy": CURATION_POLICY,
            "template_locks": _template_manifest(),
            "persona_count": len(manifest.personas),
            "page_count": len(records),
            "route_count": len(records) * 2,
            "pages": sorted(records, key=lambda item: str(item["page_id"])),
        }
        finalized, validation = finalize_build_validation(
            stage,
            generation_manifest,
            personas={persona.id: persona for persona in manifest.personas},
            browser=browser_validation,
            validation_settings=validation_settings,
        )
        stage.rename(final_dir)
        _write_reuse_index(final_dir, manifest)
        return {
            "status": "ok",
            "reused": False,
            "build_dir": str(final_dir),
            "manifest": finalized,
            "validation": validation,
            "build_stats": {
                "built_page_count": built_count,
                "reused_page_count": reused_count,
                "jobs": effective_jobs,
                "exact_build_reuse": False,
            },
        }
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
