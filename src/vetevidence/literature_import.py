from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from typing import Literal

from pydantic import BaseModel, Field

from vetevidence.models import PubMedArticle


ExportFormat = Literal["ris", "endnote", "refworks"]


class ImportedLiterature(BaseModel):
    source_id: str
    source: str = "用户导入"
    export_format: ExportFormat
    title: str
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    journal: str | None = None
    doi: str | None = None
    abstract: str | None = None
    keywords: list[str] = Field(default_factory=list)
    source_url: str | None = None
    warnings: list[str] = Field(default_factory=list)


class DuplicateMatch(BaseModel):
    imported_source_id: str
    matched_source_id: str
    reason: Literal["DOI", "标题与年份"]


class LiteratureImportResult(BaseModel):
    records: list[ImportedLiterature] = Field(default_factory=list)
    duplicates: list[DuplicateMatch] = Field(default_factory=list)
    skipped_records: int = 0
    warnings: list[str] = Field(default_factory=list)


def _clean_doi(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(
        r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)",
        "",
        value.strip(),
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.rstrip(" .;,")
    return cleaned or None


def _normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())


def _first(fields: dict[str, list[str]], *keys: str) -> str | None:
    for key in keys:
        values = fields.get(key, [])
        if values and values[0].strip():
            return values[0].strip()
    return None


def _many(fields: dict[str, list[str]], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        for value in fields.get(key, []):
            cleaned = value.strip(" ;")
            if cleaned and cleaned not in values:
                values.append(cleaned)
    return values


def _year(value: str | None) -> int | None:
    match = re.search(r"\b(19|20)\d{2}\b", value or "")
    return int(match.group(0)) if match else None


def _source_id(
    *,
    title: str,
    doi: str | None,
    year: int | None,
    authors: list[str],
) -> str:
    identity = doi or "|".join(
        (title.casefold(), str(year or ""), authors[0].casefold() if authors else "")
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"IMPORTED-{digest}"


def _to_record(
    fields: dict[str, list[str]],
    export_format: ExportFormat,
) -> ImportedLiterature | None:
    mappings: dict[ExportFormat, dict[str, tuple[str, ...]]] = {
        "ris": {
            "title": ("TI", "T1"),
            "authors": ("AU", "A1"),
            "year": ("PY", "Y1"),
            "journal": ("JO", "JF", "T2"),
            "doi": ("DO",),
            "abstract": ("AB", "N2"),
            "keywords": ("KW",),
            "url": ("UR", "L1"),
        },
        "endnote": {
            "title": ("Title",),
            "authors": ("Author", "Secondary Author"),
            "year": ("Year",),
            "journal": ("Journal", "Secondary Title"),
            "doi": ("DOI",),
            "abstract": ("Abstract",),
            "keywords": ("Keywords",),
            "url": ("URL",),
        },
        "refworks": {
            "title": ("T1",),
            "authors": ("A1",),
            "year": ("YR", "Y1"),
            "journal": ("JF", "T2"),
            "doi": ("DO",),
            "abstract": ("AB", "N2"),
            "keywords": ("K1", "KW"),
            "url": ("UL", "UR"),
        },
    }
    mapping = mappings[export_format]
    title = _first(fields, *mapping["title"])
    if not title:
        return None
    authors = _many(fields, *mapping["authors"])
    year = _year(_first(fields, *mapping["year"]))
    doi = _clean_doi(_first(fields, *mapping["doi"]))
    abstract = _first(fields, *mapping["abstract"])
    source_url = _first(fields, *mapping["url"])
    if not source_url and doi:
        source_url = f"https://doi.org/{doi}"
    warnings = []
    if not abstract:
        warnings.append("导出记录未包含摘要，不能自动提取实验细节。")
    if not doi:
        warnings.append("导出记录未包含 DOI，将使用标题与年份去重。")
    return ImportedLiterature(
        source_id=_source_id(
            title=title,
            doi=doi,
            year=year,
            authors=authors,
        ),
        export_format=export_format,
        title=title,
        authors=authors,
        year=year,
        journal=_first(fields, *mapping["journal"]),
        doi=doi,
        abstract=abstract,
        keywords=_many(fields, *mapping["keywords"]),
        source_url=source_url,
        warnings=warnings,
    )


def _parse_ris(text: str) -> tuple[list[dict[str, list[str]]], int]:
    records: list[dict[str, list[str]]] = []
    fields: defaultdict[str, list[str]] = defaultdict(list)
    active_tag: str | None = None
    skipped = 0
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        match = re.match(r"^([A-Z0-9]{2})  -\s?(.*)$", line)
        if match:
            tag, value = match.groups()
            if tag == "ER":
                if fields:
                    records.append(dict(fields))
                else:
                    skipped += 1
                fields = defaultdict(list)
                active_tag = None
                continue
            fields[tag].append(value.strip())
            active_tag = tag
        elif line.strip() and active_tag and fields[active_tag]:
            fields[active_tag][-1] = (
                f"{fields[active_tag][-1]} {line.strip()}".strip()
            )
    if fields:
        records.append(dict(fields))
    return records, skipped


def _parse_labelled(
    text: str,
    *,
    pattern: str,
    boundary_key: str,
) -> tuple[list[dict[str, list[str]]], int]:
    records: list[dict[str, list[str]]] = []
    fields: defaultdict[str, list[str]] = defaultdict(list)
    active_key: str | None = None
    skipped = 0
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        match = re.match(pattern, line)
        if match:
            key, value = match.groups()
            if key == boundary_key and fields:
                records.append(dict(fields))
                fields = defaultdict(list)
            fields[key].append(value.strip())
            active_key = key
        elif line.strip() and active_key and fields[active_key]:
            fields[active_key][-1] = (
                f"{fields[active_key][-1]} {line.strip()}".strip()
            )
        elif not line.strip() and fields:
            records.append(dict(fields))
            fields = defaultdict(list)
            active_key = None
    if fields:
        records.append(dict(fields))
    return records, skipped


def detect_export_format(text: str) -> ExportFormat:
    if re.search(r"(?m)^TY  - ", text) and re.search(r"(?m)^ER  -", text):
        return "ris"
    if re.search(r"(?m)^Reference Type:\s*", text):
        return "endnote"
    if re.search(r"(?m)^RT\s+", text) and re.search(r"(?m)^T1\s+", text):
        return "refworks"
    raise ValueError("无法识别文献导出格式；请使用 RIS、EndNote 或 RefWorks。")


def parse_literature_export(
    payload: bytes | str,
    *,
    pubmed_articles: list[PubMedArticle] | None = None,
) -> LiteratureImportResult:
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = payload.decode("gb18030")
    else:
        text = payload.lstrip("\ufeff")
    if not text.strip():
        raise ValueError("文献导出文件为空。")

    export_format = detect_export_format(text)
    if export_format == "ris":
        raw_records, skipped = _parse_ris(text)
    elif export_format == "endnote":
        raw_records, skipped = _parse_labelled(
            text,
            pattern=r"^([^:]+):\s?(.*)$",
            boundary_key="Reference Type",
        )
    else:
        raw_records, skipped = _parse_labelled(
            text,
            pattern=r"^([A-Z][A-Z0-9])\s+(.*)$",
            boundary_key="RT",
        )

    parsed: list[ImportedLiterature] = []
    warnings: list[str] = []
    for fields in raw_records:
        record = _to_record(fields, export_format)
        if record:
            parsed.append(record)
        else:
            skipped += 1
    if not parsed:
        raise ValueError("导出文件中没有包含标题的有效文献记录。")

    duplicates: list[DuplicateMatch] = []
    unique: list[ImportedLiterature] = []
    doi_index: dict[str, str] = {}
    title_index: dict[tuple[str, int | None], str] = {}
    for article in pubmed_articles or []:
        if article.doi:
            doi_index[article.doi.casefold()] = f"PMID {article.pmid}"
        title_index[(_normalize_title(article.title), article.year)] = (
            f"PMID {article.pmid}"
        )

    for record in parsed:
        matched_source_id: str | None = None
        reason: Literal["DOI", "标题与年份"] | None = None
        if record.doi and record.doi.casefold() in doi_index:
            matched_source_id = doi_index[record.doi.casefold()]
            reason = "DOI"
        else:
            title_key = (_normalize_title(record.title), record.year)
            if title_key in title_index:
                matched_source_id = title_index[title_key]
                reason = "标题与年份"
        if matched_source_id and reason:
            duplicates.append(
                DuplicateMatch(
                    imported_source_id=record.source_id,
                    matched_source_id=matched_source_id,
                    reason=reason,
                )
            )
            continue
        unique.append(record)
        if record.doi:
            doi_index[record.doi.casefold()] = record.source_id
        title_index[(_normalize_title(record.title), record.year)] = (
            record.source_id
        )

    if duplicates:
        warnings.append(f"已识别并排除 {len(duplicates)} 条重复文献。")
    return LiteratureImportResult(
        records=unique,
        duplicates=duplicates,
        skipped_records=skipped,
        warnings=warnings,
    )
