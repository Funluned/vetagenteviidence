from __future__ import annotations

from vetevidence.literature_import import (
    detect_export_format,
    parse_literature_export,
)
from vetevidence.models import PubMedArticle


RIS_EXPORT = """\
TY  - JOUR
AU  - Zhang, San
AU  - Li, Si
TI  - Synergistic activity against Streptococcus agalactiae
JO  - Veterinary Microbiology
PY  - 2025
DO  - https://doi.org/10.1000/example.1
AB  - Drug A and drug B showed synergistic activity.
KW  - synergy
KW  - Streptococcus agalactiae
UR  - https://kns.cnki.net/example
ER  -
"""


ENDNOTE_EXPORT = """\
Reference Type: Journal Article
Author: Zhang, San
Author: Li, Si
Year: 2025
Title: Synergistic activity against Streptococcus agalactiae
Journal: Veterinary Microbiology
DOI: doi:10.1000/example.1
Abstract: Drug A and drug B showed synergistic activity.
Keywords: synergy
URL: https://kns.cnki.net/example
"""


REFWORKS_EXPORT = """\
RT Journal Article
T1 Synergistic activity against Streptococcus agalactiae
A1 Zhang, San
A1 Li, Si
YR 2025
JF Veterinary Microbiology
DO 10.1000/example.1
AB Drug A and drug B showed synergistic activity.
K1 synergy
UL https://kns.cnki.net/example
"""


def test_detects_three_supported_export_formats() -> None:
    assert detect_export_format(RIS_EXPORT) == "ris"
    assert detect_export_format(ENDNOTE_EXPORT) == "endnote"
    assert detect_export_format(REFWORKS_EXPORT) == "refworks"


def test_parses_ris_and_preserves_traceable_fields() -> None:
    result = parse_literature_export(RIS_EXPORT.encode("utf-8"))

    assert len(result.records) == 1
    record = result.records[0]
    assert record.export_format == "ris"
    assert record.title.startswith("Synergistic activity")
    assert record.authors == ["Zhang, San", "Li, Si"]
    assert record.year == 2025
    assert record.doi == "10.1000/example.1"
    assert record.source_url == "https://kns.cnki.net/example"
    assert record.abstract.startswith("Drug A")
    assert record.keywords == ["synergy", "Streptococcus agalactiae"]


def test_endnote_and_refworks_map_to_same_identity() -> None:
    endnote = parse_literature_export(ENDNOTE_EXPORT).records[0]
    refworks = parse_literature_export(REFWORKS_EXPORT).records[0]

    assert endnote.source_id == refworks.source_id
    assert endnote.doi == refworks.doi
    assert endnote.title == refworks.title


def test_deduplicates_against_pubmed_by_doi() -> None:
    pubmed = PubMedArticle(
        pmid="123",
        title="A differently punctuated title",
        year=2025,
        doi="10.1000/example.1",
        source_url="https://pubmed.ncbi.nlm.nih.gov/123/",
    )

    result = parse_literature_export(
        RIS_EXPORT,
        pubmed_articles=[pubmed],
    )

    assert result.records == []
    assert result.duplicates[0].matched_source_id == "PMID 123"
    assert result.duplicates[0].reason == "DOI"
    assert "排除 1 条重复文献" in result.warnings[0]


def test_missing_abstract_and_doi_are_explicit_warnings() -> None:
    payload = """\
TY  - JOUR
TI  - A record without abstract
PY  - 2024
ER  -
"""

    record = parse_literature_export(payload).records[0]

    assert record.abstract is None
    assert any("未包含摘要" in warning for warning in record.warnings)
    assert any("未包含 DOI" in warning for warning in record.warnings)


def test_rejects_empty_or_unknown_export() -> None:
    try:
        parse_literature_export("")
    except ValueError as exc:
        assert "为空" in str(exc)
    else:
        raise AssertionError("empty payload must be rejected")

    try:
        parse_literature_export("plain text")
    except ValueError as exc:
        assert "无法识别" in str(exc)
    else:
        raise AssertionError("unknown payload must be rejected")
