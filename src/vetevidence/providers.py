from __future__ import annotations

from typing import Protocol

from vetevidence.answering import build_cited_answer
from vetevidence.extraction import extract_evidence
from vetevidence.models import (
    CitedAnswer,
    EvidenceRecord,
    PubMedArticle,
)


class EvidenceProvider(Protocol):
    """Replaceable extraction and answering boundary."""

    name: str

    def extract(self, article: PubMedArticle) -> EvidenceRecord: ...

    def answer(
        self,
        question: str,
        evidence_records: list[EvidenceRecord],
    ) -> CitedAnswer: ...


class RuleBasedEvidenceProvider:
    """Deterministic fallback that works without an LLM API key."""

    name = "rules_v1"

    def extract(self, article: PubMedArticle) -> EvidenceRecord:
        return extract_evidence(article)

    def answer(
        self,
        question: str,
        evidence_records: list[EvidenceRecord],
    ) -> CitedAnswer:
        return build_cited_answer(question, evidence_records)
