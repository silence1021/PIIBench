"""Strict, page-agnostic schemas for synthetic persona manifests."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from defipi.config import load_config_file
from defipi.configuration.schema import Field, Problem, validate_config


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")
_EMAIL_RE = re.compile(r"^[A-Z0-9._%+-]+@([A-Z0-9.-]+)$", re.IGNORECASE)
_PHONE_RE = re.compile(r"^\+?[0-9][0-9() .-]*[0-9]$")
_ALLOWED_EMAIL_DOMAINS = {
    "example.com",
    "example.net",
    "example.org",
    "example.edu",
}
# DUSK domains verified without public A, MX, or NS records on 2026-07-13.
# The explicit allowlist keeps the schema deterministic without allowing
# arbitrary real-world domains.
_AUDITED_DUSK_EMAIL_DOMAINS = frozenset(
    """
    abaisu.kz
    alfarabi.edu
    artemismed.ro
    atluniv.edu
    balticuni.eu
    biscos.ac.th
    breakwater.edu
    chandrai.edu
    charlesworth.edu
    chem.edu
    chem.edu.sg
    ciu.edu.cn
    coimbra.edu
    cordova.edu
    cphuniv.dk
    crestwood.edu
    dahrauniv.sa
    efu.edu
    everlight.edu
    fakeschool.fr
    fict.edu
    fiction.edu
    fictional.edu
    fictitious.edu
    fictmail.org
    galata.edu
    gandhiuniversity.in
    grandducal.edu
    grandville.edu
    greenwood.edu
    gunadharma.ac.id
    haenam.edu
    haifauniv.org
    hanwook.edu
    hazim.edu
    hellenic.edu
    highvale.ca
    hohenburg.edu
    huangxi.edu
    hvuniv.edu
    imperialvalley.sg
    ionianuni.gr
    kgu.edu
    khi.edu
    kingsbridge.edu
    kkuniv.sa
    lunduni.se
    mathpl.edu
    mehtauniv.edu
    natal.edu
    oncativo.edu
    osloglobal.edu
    pragueuni.cz
    qnu.edu.qa
    redmont.edu
    rioplata.ar
    riyas.qa
    roma.edu
    rutherford.edu
    sainte-hubert.edu
    somerset.edu
    ssu.ac.id
    starhill.hr
    stockholmu.se
    tallinnuni.ee
    telstead.edu
    terrasaltas.edu
    transdanubian.hu
    transylvania.ro
    uni-metro.edu
    uni-nom.si
    uni.mt
    univ.ie
    univmail.cl
    valmont.edu
    vgu.edu
    vietuni.edu
    volgastate.ru
    vtech.edu
    windmillu.edu
    zahra.edu
    zurichmed.edu
    """.split()
)
# Additional DUSK holdout domains checked with the same A/MX/NS rule on
# 2026-09-23. Query results: data/dusk/academic_holdout70_email_audit.json.
_AUDITED_DUSK_HOLDOUT_EMAIL_DOMAINS = frozenset(
    """
    alkout.edu alzahir.om andes.cl aoraki.edu auroranorte.br azul.edu
    baltic.edu chalermchai.edu chem.univ ctu.ac.za daehan.edu dhu.ae
    dunav.edu faisal.edu fauxuniv.fr fictionalmail.com fictionmail.ie
    fictmail.edu fictu.nl kronwick.edu lisbonpoly.edu nagakura.edu
    namviet.edu novamos.ru oakmail.edu orvieto.edu penbridge.edu prado.edu
    qau.edu rannu.edu riverfront.edu rotterdam.edu royalu.lu royaluniv.be
    shamal.edu shangdu.edu skjolden.edu stellaria.edu trnovouni.si ugbay.is
    unisol.edu univsantalucia.edu uos.br viennaguni.edu
    """.split()
)
_FORBIDDEN_KEYS = {
    "case_id",
    "question",
    "ideal",
    "defense_fragment",
    "injection_position",
    "attack_instruction",
    "rule_text",
}
_FORBIDDEN_TEXT = (
    "piiguard",
    "data-piiguard",
    "defense_fragment",
    "injection_position",
    "ignore previous instructions",
    "system directive",
    "[instruction]",
)
_PERSONA_KEYS = {
    "id",
    "name",
    "title",
    "department",
    "institution",
    "email",
    "phone",
    "address",
    "bio",
    "research_interests",
    "recruiting_note",
    "publications",
    "projects",
    "teaching",
    "news",
    "avatar",
}


@dataclass(frozen=True)
class Publication:
    title: str
    authors: str
    venue: str
    year: str


@dataclass(frozen=True)
class Project:
    name: str
    description: str


@dataclass(frozen=True)
class TeachingItem:
    course: str
    term: str
    description: str


@dataclass(frozen=True)
class NewsItem:
    date: str
    text: str


@dataclass(frozen=True)
class SyntheticPersona:
    id: str
    name: str
    title: str
    department: str
    institution: str
    email: str
    phone: str
    address: str
    bio: tuple[str, ...]
    research_interests: tuple[str, ...]
    recruiting_note: str
    publications: tuple[Publication, ...]
    projects: tuple[Project, ...]
    teaching: tuple[TeachingItem, ...]
    news: tuple[NewsItem, ...]
    avatar: Path

    def canonical_dict(self) -> dict[str, Any]:
        document = asdict(self)
        document["avatar"] = self.avatar.name
        return document

    def expected_visible_strings(self) -> tuple[str, ...]:
        values = list(self.expected_home_visible_strings())
        for item in self.publications:
            values.extend((item.title, item.authors, item.venue, item.year))
        return tuple(value for value in values if value)

    def expected_home_visible_strings(self) -> tuple[str, ...]:
        values: list[str] = [
            self.name,
            self.title,
            self.department,
            self.institution,
            self.email,
            self.phone,
            self.address,
            *self.bio,
            *self.research_interests,
            self.recruiting_note,
        ]
        for item in self.projects:
            values.extend((item.name, item.description))
        for item in self.teaching:
            values.extend((item.course, item.term, item.description))
        for item in self.news:
            values.extend((item.date, item.text))
        return tuple(value for value in values if value)


@dataclass(frozen=True)
class SyntheticPersonaManifest:
    version: int
    personas: tuple[SyntheticPersona, ...]
    path: Path

    def canonical_json(self) -> str:
        payload = {
            "version": self.version,
            "personas": [persona.canonical_dict() for persona in self.personas],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return dict(value)


def _text(value: Any, label: str) -> str:
    if not isinstance(value, (str, int)):
        raise ValueError(f"{label} must be a non-empty string")
    normalized = " ".join(str(value).split()).strip()
    if not normalized:
        raise ValueError(f"{label} must be a non-empty string")
    lowered = normalized.lower()
    for token in _FORBIDDEN_TEXT:
        if token in lowered:
            raise ValueError(f"{label} contains forbidden defense/instruction text: {token!r}")
    return normalized


def _optional_text(value: Any, label: str) -> str:
    if value is None or value == "":
        return ""
    return _text(value, label)


def _text_list(value: Any, label: str, *, required: bool = False) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    elif value is None:
        items = []
    else:
        raise ValueError(f"{label} must be a string or list of strings")
    normalized = tuple(_text(item, f"{label}[{index}]") for index, item in enumerate(items))
    if required and not normalized:
        raise ValueError(f"{label} must not be empty")
    return normalized


def _reject_forbidden_keys(value: Any, label: str = "manifest") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().lower()
            if normalized in _FORBIDDEN_KEYS:
                raise ValueError(f"{label} contains forbidden experiment/defense key: {key!r}")
            _reject_forbidden_keys(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_keys(child, f"{label}[{index}]")


def _item_list(
    value: Any,
    label: str,
    fields: tuple[str, ...],
    factory: Any,
) -> tuple[Any, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    output = []
    for index, raw in enumerate(value):
        item = _mapping(raw, f"{label}[{index}]")
        unknown = sorted(set(item) - set(fields))
        if unknown:
            raise ValueError(f"{label}[{index}] has unsupported fields: {', '.join(unknown)}")
        output.append(factory(*(_optional_text(item.get(field), f"{label}[{index}].{field}") for field in fields)))
    return tuple(output)


def _validate_synthetic_contacts(email: str, phone: str, label: str) -> None:
    match = _EMAIL_RE.fullmatch(email)
    if not match:
        raise ValueError(f"{label}.email is not a valid email address")
    domain = match.group(1).lower()
    allowed = (
        domain in _ALLOWED_EMAIL_DOMAINS
        or domain in _AUDITED_DUSK_EMAIL_DOMAINS
        or domain in _AUDITED_DUSK_HOLDOUT_EMAIL_DOMAINS
        or any(domain.endswith("." + item) for item in _ALLOWED_EMAIL_DOMAINS)
        or domain.endswith((".test", ".invalid"))
    )
    if not allowed:
        raise ValueError(
            f"{label}.email must use an approved synthetic domain; got {domain!r}"
        )
    digits = "".join(character for character in phone if character.isdigit())
    if not _PHONE_RE.fullmatch(phone) or not 7 <= len(digits) <= 15:
        raise ValueError(f"{label}.phone must be a valid 7-15 digit phone number")


def _parse_persona(raw: Any, index: int, manifest_dir: Path) -> SyntheticPersona:
    label = f"personas[{index}]"
    data = _mapping(raw, label)
    unknown = sorted(set(data) - _PERSONA_KEYS)
    if unknown:
        raise ValueError(f"{label} has unsupported fields: {', '.join(unknown)}")

    persona_id = _text(data.get("id"), f"{label}.id").lower()
    if not _ID_RE.fullmatch(persona_id):
        raise ValueError(f"{label}.id must match {_ID_RE.pattern!r}")
    email = _text(data.get("email"), f"{label}.email")
    phone = _text(data.get("phone"), f"{label}.phone")
    _validate_synthetic_contacts(email, phone, label)

    avatar_raw = _text(data.get("avatar"), f"{label}.avatar")
    avatar_path = (manifest_dir / avatar_raw).resolve()
    try:
        avatar_path.relative_to(manifest_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"{label}.avatar must stay inside the persona manifest directory") from exc
    if not avatar_path.is_file():
        raise FileNotFoundError(f"{label}.avatar does not exist: {avatar_path}")
    if avatar_path.suffix.lower() not in {".svg", ".png", ".jpg", ".jpeg", ".webp"}:
        raise ValueError(f"{label}.avatar must be an SVG, PNG, JPEG, or WebP image")

    return SyntheticPersona(
        id=persona_id,
        name=_text(data.get("name"), f"{label}.name"),
        title=_optional_text(data.get("title"), f"{label}.title"),
        department=_text(data.get("department"), f"{label}.department"),
        institution=_text(data.get("institution"), f"{label}.institution"),
        email=email,
        phone=phone,
        address=_text(data.get("address"), f"{label}.address"),
        bio=_text_list(data.get("bio"), f"{label}.bio"),
        research_interests=_text_list(
            data.get("research_interests"),
            f"{label}.research_interests",
            required=True,
        ),
        recruiting_note=_optional_text(
            data.get("recruiting_note"),
            f"{label}.recruiting_note",
        ),
        publications=_item_list(
            data.get("publications"),
            f"{label}.publications",
            ("title", "authors", "venue", "year"),
            Publication,
        ),
        projects=_item_list(
            data.get("projects"),
            f"{label}.projects",
            ("name", "description"),
            Project,
        ),
        teaching=_item_list(
            data.get("teaching"),
            f"{label}.teaching",
            ("course", "term", "description"),
            TeachingItem,
        ),
        news=_item_list(
            data.get("news"),
            f"{label}.news",
            ("date", "text"),
            NewsItem,
        ),
        avatar=avatar_path,
    )


def load_persona_manifest(path: str | Path) -> SyntheticPersonaManifest:
    manifest_path = Path(path).expanduser().resolve()
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Academic persona manifest not found: {manifest_path}")
    root = load_config_file(manifest_path)
    text = lambda required=False: Field(str, required=required, nullable=not required)
    item_fields = {
        "publications": ("title", "authors", "venue", "year"),
        "projects": ("name", "description"),
        "teaching": ("course", "term", "description"),
        "news": ("date", "text"),
    }
    persona_fields = {name: text(True) for name in (
        "id", "name", "department", "institution", "email", "phone", "address", "avatar")}
    persona_fields.update({name: text() for name in ("title", "recruiting_note")})
    for name in ("bio", "research_interests"):
        persona_fields[name] = Field((str, list), required=name == "research_interests",
                                    nullable=name == "bio", items=Field(str))
    for name, fields in item_fields.items():
        persona_fields[name] = Field(list, required=False, nullable=True,
            items=Field(dict, schema={field: Field((str, int), required=False, nullable=True)
                                     if field == "year" else text() for field in fields}))
    parsed = []
    def semantic_rules(data, problems):
        raw = data.get("personas")
        if type(raw) is not list:
            return
        if not raw:
            problems.append(Problem("personas", "must not be empty"))
        seen = set()
        for index, record in enumerate(raw):
            if type(record) is dict and type(record.get("id")) is str:
                persona_id = record["id"].strip().lower()
                if persona_id in seen:
                    problems.append(Problem(f"personas[{index}].id", "duplicate persona id"))
                seen.add(persona_id)
            try:
                parsed.append(_parse_persona(record, index, manifest_path.parent))
            except (ValueError, TypeError, FileNotFoundError) as exc:
                problems.append(Problem(f"personas[{index}]", str(exc)))
    validate_config(root, {"version": Field(int, choices=(1,)),
        "personas": Field(list, items=Field(dict, schema=persona_fields))}, rules=(semantic_rules,))
    personas = tuple(parsed)
    return SyntheticPersonaManifest(version=1, personas=personas, path=manifest_path)


def iter_persona_text(persona: SyntheticPersona) -> Iterable[str]:
    """Yield normalized synthetic content for parity and safety audits."""

    yield from persona.expected_visible_strings()
