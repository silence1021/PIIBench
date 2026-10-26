"""Pinned Jekyll backends for defense-free synthetic academic homepages."""

from __future__ import annotations

import html
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..synthetic_personas import SyntheticPersona


@dataclass(frozen=True)
class TemplateLock:
    backend_id: str
    repository: str
    tag: str
    cache_dirname: str
    docker_image: str
    docker_workdir: str


ACADEMIC_PAGES_LOCK = TemplateLock(
    backend_id="academic_pages",
    repository="https://github.com/academicpages/academicpages.github.io.git",
    tag="v0.9",
    cache_dirname="academic_pages-v0.9",
    docker_image="piibench-academic-pages:v0.9",
    docker_workdir="/usr/src/app",
)
AL_FOLIO_LOCK = TemplateLock(
    backend_id="al_folio",
    repository="https://github.com/alshedivat/al-folio.git",
    tag="v0.16.3",
    cache_dirname="al_folio-v0.16.3",
    docker_image="amirpourmand/al-folio:v0.16.3",
    docker_workdir="/srv/jekyll",
)
TEMPLATE_LOCKS = {
    ACADEMIC_PAGES_LOCK.backend_id: ACADEMIC_PAGES_LOCK,
    AL_FOLIO_LOCK.backend_id: AL_FOLIO_LOCK,
}
BACKEND_IDS = tuple(TEMPLATE_LOCKS)


_AL_FOLIO_HEAD = """<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{{ site.description | strip_html | escape }}">
<title>{% if site.title == 'blank' %}{{ site.first_name }} {{ site.middle_name }} {{ site.last_name }}{% else %}{{ site.title }}{% endif %}</title>
<link rel="stylesheet" href="{{ '/assets/css/bootstrap.min.css' | relative_url }}">
<link rel="stylesheet" href="{{ '/assets/css/mdb.min.css' | relative_url }}">
<link rel="stylesheet" href="{{ '/assets/css/academicons.min.css' | relative_url }}">
<link rel="stylesheet" href="{{ '/assets/css/scholar-icons.css' | relative_url }}">
<link rel="stylesheet" href="{{ '/assets/css/jekyll-pygments-themes-github.css' | relative_url }}" id="highlight_theme_light">
<link rel="stylesheet" href="{{ '/assets/css/main.css' | relative_url }}">
{% if site.enable_darkmode %}
<script src="{{ '/assets/js/theme.js' | relative_url }}"></script>
<link rel="stylesheet" href="{{ '/assets/css/jekyll-pygments-themes-native.css' | relative_url }}" media="none" id="highlight_theme_dark">
<script>initTheme();</script>
{% endif %}
"""

_AL_FOLIO_SCRIPTS = """{% if site.back_to_top %}
<script src="{{ '/assets/js/vanilla-back-to-top.min.js' | relative_url }}"></script>
<script>addBackToTop();</script>
{% endif %}
"""

_ACADEMIC_CUSTOM_HEAD = """{% include base_path %}
<link rel="stylesheet" href="{{ base_path }}/assets/css/academicons.min.css">
<link rel="stylesheet" href="{{ base_path }}/assets/css/fontawesome.css">
<meta name="theme-color" content="var(--global-bg-color)">
"""


def _dump_yaml(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000),
        encoding="utf-8",
    )


def _front_matter(data: dict[str, Any], body: str) -> str:
    header = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000).strip()
    return f"---\n{header}\n---\n\n{body.strip()}\n"


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _reset_directory(path: Path) -> None:
    _remove_path(path)
    path.mkdir(parents=True, exist_ok=True)


def _copy_pristine_template(template_root: Path, workspace: Path) -> None:
    if not template_root.is_dir():
        raise FileNotFoundError(f"Prepared template cache is missing: {template_root}")
    shutil.copytree(
        template_root,
        workspace,
        ignore=shutil.ignore_patterns(
            ".git",
            "_site",
            ".jekyll-cache",
            ".sass-cache",
            ".asset-cache",
            "node_modules",
        ),
    )


def _paragraphs(values: tuple[str, ...]) -> str:
    return "\n".join(f"<p>{html.escape(value)}</p>" for value in values)


def _persona_description(persona: SyntheticPersona) -> str:
    return ", ".join(
        value for value in (persona.title, persona.department, persona.institution) if value
    )


def render_shared_homepage(persona: SyntheticPersona) -> str:
    """Render the exact body shared by both template backends."""

    position = [
        '<section id="academic-position">',
        "<h2>Position</h2>",
    ]
    if persona.title:
        position.append(f"<p>{html.escape(persona.title)}</p>")
    position.extend(
        [
            f"<p>{html.escape(persona.department)}, {html.escape(persona.institution)}</p>",
            "</section>",
        ]
    )
    sections = [*position]
    if persona.bio:
        sections.extend(
            [
                '<section id="about">',
                "<h2>About</h2>",
                _paragraphs(persona.bio),
                "</section>",
            ]
        )
    sections.extend(
        [
            '<section id="research-interests">',
            "<h2>Research interests</h2>",
            "<ul>",
            *(f"<li>{html.escape(item)}</li>" for item in persona.research_interests),
            "</ul>",
            "</section>",
        ]
    )
    if persona.recruiting_note:
        sections.extend(
            [
                '<section id="prospective-students">',
                "<h2>Prospective students</h2>",
                f"<p>{html.escape(persona.recruiting_note)}</p>",
                "</section>",
            ]
        )
    sections.extend(
        [
            '<section id="contact">',
            "<h2>Contact</h2>",
            "<dl>",
            "<dt>Email</dt>",
            f'<dd><a href="mailto:{html.escape(persona.email, quote=True)}">{html.escape(persona.email)}</a></dd>',
            "<dt>Phone</dt>",
            f'<dd><a href="tel:{html.escape(persona.phone, quote=True)}">{html.escape(persona.phone)}</a></dd>',
            "<dt>Office</dt>",
            f"<dd>{html.escape(persona.address)}</dd>",
            "</dl>",
            "</section>",
        ]
    )
    if persona.projects:
        sections.extend(['<section id="projects">', "<h2>Projects</h2>", "<ul>"])
        for item in persona.projects:
            sections.append(
                f"<li><strong>{html.escape(item.name)}</strong>"
                + (f": {html.escape(item.description)}" if item.description else "")
                + "</li>"
            )
        sections.extend(["</ul>", "</section>"])
    if persona.teaching:
        sections.extend(['<section id="teaching">', "<h2>Teaching</h2>", "<ul>"])
        for item in persona.teaching:
            suffix = " ".join(value for value in (item.term, item.description) if value)
            sections.append(
                f"<li><strong>{html.escape(item.course)}</strong>"
                + (f" — {html.escape(suffix)}" if suffix else "")
                + "</li>"
            )
        sections.extend(["</ul>", "</section>"])
    if persona.news:
        sections.extend(['<section id="news">', "<h2>News</h2>", "<ul>"])
        for item in persona.news:
            sections.append(
                "<li>"
                + (f"<time>{html.escape(item.date)}</time> — " if item.date else "")
                + html.escape(item.text)
                + "</li>"
            )
        sections.extend(["</ul>", "</section>"])
    return "\n".join(sections)


def render_shared_publications(persona: SyntheticPersona) -> str:
    """Render sourced publication records on their own page."""

    sections = ['<section id="selected-publications">', "<h2>Publications</h2>"]
    if persona.publications:
        sections.append("<ol>")
        for item in persona.publications:
            meta = ", ".join(value for value in (item.authors, item.venue, item.year) if value)
            sections.append(
                f"<li><strong>{html.escape(item.title)}</strong>"
                + (f". {html.escape(meta)}" if meta else "")
                + "</li>"
            )
        sections.append("</ol>")
    else:
        sections.append("<p>No publication information is available in the DUSK source.</p>")
    sections.append("</section>")
    return "\n".join(sections)


class AcademicTemplateBackend(ABC):
    lock: TemplateLock

    def __init__(self, template_root: str | Path):
        self.template_root = Path(template_root).expanduser().resolve()

    @property
    def backend_id(self) -> str:
        return self.lock.backend_id

    def materialize(self, persona: SyntheticPersona, workspace: str | Path) -> Path:
        workspace_path = Path(workspace).expanduser().resolve()
        if workspace_path.exists():
            raise FileExistsError(f"Backend workspace already exists: {workspace_path}")
        _copy_pristine_template(self.template_root, workspace_path)
        self._materialize(persona, workspace_path)
        return workspace_path

    @abstractmethod
    def _materialize(self, persona: SyntheticPersona, workspace: Path) -> None:
        raise NotImplementedError

    def build(self, workspace: str | Path) -> Path:
        workspace_path = Path(workspace).expanduser().resolve()
        destination = workspace_path / "_site"
        _remove_path(destination)
        uid = os.getuid() if hasattr(os, "getuid") else 1000
        gid = os.getgid() if hasattr(os, "getgid") else 1000
        command = [
            "docker",
            "run",
            "--rm",
            "--init",
            "--network=none",
            "--user",
            f"{uid}:{gid}",
            "-e",
            "HOME=/tmp",
            "-e",
            "JEKYLL_ENV=production",
            "-e",
            "SOURCE_DATE_EPOCH=1704067200",
            "-v",
            f"{workspace_path}:{self.lock.docker_workdir}",
            "-w",
            self.lock.docker_workdir,
            self.lock.docker_image,
            "bundle",
            "exec",
            "jekyll",
            "build",
            "--destination",
            f"{self.lock.docker_workdir}/_site",
        ]
        if self.backend_id == "academic_pages":
            command.extend(["--config", "_config.yml,_config_docker.yml"])
        completed = subprocess.run(command, text=True, capture_output=True, check=False)
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"{self.backend_id} Jekyll build failed: {detail}")
        if not (destination / "index.html").is_file():
            raise RuntimeError(f"{self.backend_id} build did not produce _site/index.html")
        return destination


class AcademicPagesBackend(AcademicTemplateBackend):
    lock = ACADEMIC_PAGES_LOCK

    def _materialize(self, persona: SyntheticPersona, workspace: Path) -> None:
        for relative in (
            "_drafts",
            "_pages",
            "_portfolio",
            "_posts",
            "_publications",
            "_talks",
            "_teaching",
            "files",
            "markdown_generator",
            "talkmap",
        ):
            _remove_path(workspace / relative)
        _reset_directory(workspace / "_pages")
        _reset_directory(workspace / "images")

        avatar_name = "persona-avatar" + persona.avatar.suffix.lower()
        shutil.copy2(persona.avatar, workspace / "images" / avatar_name)
        body = render_shared_homepage(persona)
        (workspace / "_pages" / "about.md").write_text(
            _front_matter(
                {
                    "permalink": "/",
                    "title": persona.name,
                    "author_profile": True,
                },
                body,
            ),
            encoding="utf-8",
        )
        (workspace / "_pages" / "publications.md").write_text(
            _front_matter(
                {
                    "permalink": "/publications/",
                    "title": "Publications",
                    "author_profile": True,
                },
                render_shared_publications(persona),
            ),
            encoding="utf-8",
        )

        config_path = workspace / "_config.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        author = {
            "avatar": avatar_name,
            "name": persona.name,
            "location": persona.address,
            "employer": persona.institution,
            "email": persona.email,
        }
        if persona.title:
            author["bio"] = persona.title
        config.update(
            {
                "locale": "en-US",
                "title": persona.name,
                "name": persona.name,
                "description": _persona_description(persona),
                "url": "",
                "baseurl": f"/academic_pages/{persona.id}",
                "repository": "",
                "breadcrumbs": False,
                "comments": {"provider": False},
                "analytics": {"provider": False},
                "social": {"type": "Person", "name": persona.name, "links": []},
                "author": author,
            }
        )
        if isinstance(config.get("atom_feed"), dict):
            config["atom_feed"]["hide"] = True
        _dump_yaml(config_path, config)
        _dump_yaml(
            workspace / "_data" / "navigation.yml",
            {
                "main": [
                    {"title": "Home", "url": "/"},
                    {"title": "Publications", "url": "/publications/"},
                ]
            },
        )
        _dump_yaml(workspace / "_data" / "authors.yml", {})
        (workspace / "_includes" / "head" / "custom.html").write_text(
            _ACADEMIC_CUSTOM_HEAD,
            encoding="utf-8",
        )
        (workspace / "_includes" / "footer" / "custom.html").write_text("", encoding="utf-8")


class AlFolioBackend(AcademicTemplateBackend):
    lock = AL_FOLIO_LOCK

    def _materialize(self, persona: SyntheticPersona, workspace: Path) -> None:
        for relative in (
            "_books",
            "_news",
            "_pages",
            "_posts",
            "_projects",
            "_teachings",
            "assets/audio",
            "assets/bibliography",
            "assets/html",
            "assets/img",
            "assets/json",
            "assets/jupyter",
            "assets/pdf",
            "assets/plotly",
            "assets/video",
        ):
            _remove_path(workspace / relative)
        _reset_directory(workspace / "_pages")
        _reset_directory(workspace / "_bibliography")
        _reset_directory(workspace / "assets" / "img")
        (workspace / "_bibliography" / "papers.bib").write_text("", encoding="utf-8")

        avatar_name = "persona-avatar" + persona.avatar.suffix.lower()
        shutil.copy2(persona.avatar, workspace / "assets" / "img" / avatar_name)
        body = render_shared_homepage(persona)
        subtitle = " · ".join(
            value for value in (persona.title, persona.department, persona.institution) if value
        )
        more_info = "".join(
            f"<p>{html.escape(value)}</p>"
            for value in (persona.title, persona.department, persona.institution)
            if value
        )
        (workspace / "_pages" / "about.md").write_text(
            _front_matter(
                {
                    "layout": "about",
                    "title": "home",
                    "permalink": "/",
                    "subtitle": subtitle,
                    "profile": {
                        "align": "right",
                        "image": avatar_name,
                        "image_circular": False,
                        "more_info": more_info,
                    },
                    "selected_papers": False,
                    "social": False,
                    "announcements": {"enabled": False},
                    "latest_posts": {"enabled": False},
                },
                body,
            ),
            encoding="utf-8",
        )
        (workspace / "_pages" / "publications.md").write_text(
            _front_matter(
                {
                    "layout": "page",
                    "title": "publications",
                    "permalink": "/publications/",
                    "nav": True,
                    "nav_order": 2,
                },
                render_shared_publications(persona),
            ),
            encoding="utf-8",
        )

        config_path = workspace / "_config.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        config.update(
            {
                "title": "blank",
                "first_name": persona.name,
                "middle_name": "",
                "last_name": "",
                "contact_note": "",
                "description": _persona_description(persona),
                "footer_text": "",
                "keywords": "academic profile",
                "url": "",
                "baseurl": f"/al_folio/{persona.id}",
                "last_updated": False,
                "search_enabled": False,
                "external_sources": [],
                "enable_masonry": False,
                "enable_math": False,
                "enable_tooltips": False,
                "enable_darkmode": True,
                "enable_navbar_social": False,
                "enable_medium_zoom": False,
                "enable_google_analytics": False,
                "enable_cronitor_analytics": False,
                "enable_pirsch_analytics": False,
                "enable_openpanel_analytics": False,
                "enable_publication_thumbnails": False,
                "external_links": {"enabled": False},
                "imagemagick": {"enabled": False},
                "repo_trophies": {"enabled": False},
                "enable_publication_badges": {
                    "altmetric": False,
                    "dimensions": False,
                    "google_scholar": False,
                    "inspirehep": False,
                },
                "newsletter": {"enabled": False},
            }
        )
        _dump_yaml(config_path, config)
        _dump_yaml(workspace / "_data" / "socials.yml", {})
        _dump_yaml(workspace / "_data" / "coauthors.yml", {})
        _dump_yaml(workspace / "_data" / "citations.yml", {})
        _dump_yaml(workspace / "_data" / "repositories.yml", {})
        (workspace / "_includes" / "head.liquid").write_text(_AL_FOLIO_HEAD, encoding="utf-8")
        (workspace / "_includes" / "scripts.liquid").write_text(
            _AL_FOLIO_SCRIPTS,
            encoding="utf-8",
        )


def make_backend(backend_id: str, template_cache_root: str | Path) -> AcademicTemplateBackend:
    normalized = str(backend_id).strip().lower()
    lock = TEMPLATE_LOCKS.get(normalized)
    if lock is None:
        raise ValueError(f"Unknown academic page backend: {backend_id!r}")
    template_root = Path(template_cache_root).expanduser().resolve() / lock.cache_dirname
    if normalized == "academic_pages":
        return AcademicPagesBackend(template_root)
    return AlFolioBackend(template_root)
