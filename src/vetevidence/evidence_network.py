from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vetevidence.database_connectors import IdentifierMapping, ProvenanceRecord


class EvidenceModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class EvidenceType(StrEnum):
    EXPERIMENTAL = "experimental"
    CURATED_DATABASE = "curated_database"
    TEXT_MINED = "text_mined"
    COMPUTATIONAL_PREDICTION = "computational_prediction"


class EvidenceNode(EvidenceModel):
    node_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    node_type: Literal["protein", "gene", "identifier", "pathway", "term"]
    taxon_id: int | None = Field(default=None, ge=1)


class SourceTrace(EvidenceModel):
    source_name: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    source_version: str | None = None
    retrieved_at_utc: str | None = None
    raw_response_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class EvidenceEdge(EvidenceModel):
    source_node_id: str = Field(min_length=1)
    target_node_id: str = Field(min_length=1)
    relationship: str = Field(min_length=1)
    evidence_type: EvidenceType
    evidence_channel: str = Field(min_length=1)
    channel_score: float = Field(ge=0, le=1)
    ranking_score: float | None = Field(default=None, ge=0, le=1)
    ranking_score_role: Literal["ranking_only"] = "ranking_only"
    trace: SourceTrace

    @model_validator(mode="after")
    def combined_score_cannot_be_evidence_channel(self) -> EvidenceEdge:
        if self.evidence_channel.casefold() in {
            "combined",
            "combined_score",
            "score",
        }:
            raise ValueError(
                "STRING combined score is ranking-only and cannot be evidence."
            )
        return self


class EnrichmentEvidence(EvidenceModel):
    term_id: str
    term_name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    gene_ids: tuple[str, ...] = ()
    hit_count: int | None = Field(default=None, ge=0)
    input_total: int | None = Field(default=None, ge=0)
    background_hit_count: int | None = Field(default=None, ge=0)
    background_total: int | None = Field(default=None, ge=0)
    p_value: float = Field(ge=0, le=1)
    bh_adjusted_p_value: float | None = Field(default=None, ge=0, le=1)
    correction_source: Literal[
        "source_reported",
        "not_reported",
    ]
    fold_enrichment: float | None = Field(default=None, ge=0)
    taxon_id: int = Field(ge=1)
    evidence_type: EvidenceType = EvidenceType.COMPUTATIONAL_PREDICTION
    trace: SourceTrace


class RankedRelationship(EvidenceModel):
    source_node_id: str = Field(min_length=1)
    target_node_id: str = Field(min_length=1)
    combined_score: float = Field(ge=0, le=1)
    role: Literal["ranking_only"] = "ranking_only"
    trace: SourceTrace


class EvidenceNetwork(EvidenceModel):
    nodes: tuple[EvidenceNode, ...]
    edges: tuple[EvidenceEdge, ...]
    rankings: tuple[RankedRelationship, ...]
    enrichment: tuple[EnrichmentEvidence, ...] = ()
    warnings: tuple[str, ...] = ()


STRING_CHANNELS: dict[
    str,
    tuple[str, EvidenceType],
] = {
    "experimental_score": ("experimental", EvidenceType.EXPERIMENTAL),
    "database_score": ("database", EvidenceType.CURATED_DATABASE),
    "text_mining_score": ("text_mining", EvidenceType.TEXT_MINED),
    "neighborhood_score": (
        "gene_neighborhood",
        EvidenceType.COMPUTATIONAL_PREDICTION,
    ),
    "fusion_score": (
        "gene_fusion",
        EvidenceType.COMPUTATIONAL_PREDICTION,
    ),
    "phylogenetic_profile_score": (
        "phylogenetic_profile",
        EvidenceType.COMPUTATIONAL_PREDICTION,
    ),
    "coexpression_score": (
        "coexpression",
        EvidenceType.COMPUTATIONAL_PREDICTION,
    ),
}


def benjamini_hochberg(p_values: Sequence[float]) -> tuple[float, ...]:
    """Return BH-adjusted p-values in the original order."""

    values = tuple(float(value) for value in p_values)
    if any(value < 0 or value > 1 for value in values):
        raise ValueError("All p-values must be between 0 and 1.")
    count = len(values)
    if count == 0:
        return ()
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    adjusted = [1.0] * count
    running_min = 1.0
    for reverse_rank in range(count - 1, -1, -1):
        original_index, value = ordered[reverse_rank]
        rank = reverse_rank + 1
        running_min = min(running_min, value * count / rank)
        adjusted[original_index] = min(1.0, running_min)
    return tuple(adjusted)


def build_evidence_network(
    string_records: Sequence[Mapping[str, Any]],
    *,
    string_provenance: ProvenanceRecord | None = None,
    string_mappings: Sequence[IdentifierMapping] = (),
    enrichment_records: Sequence[Mapping[str, Any]] = (),
    enrichment_provenance: ProvenanceRecord | None = None,
) -> EvidenceNetwork:
    """Normalize STRING channels and enrichment output into an evidence graph."""

    nodes: dict[str, EvidenceNode] = {}
    edges: list[EvidenceEdge] = []
    rankings: dict[tuple[str, str], RankedRelationship] = {}
    warnings: list[str] = []

    string_taxon_ids = _record_taxon_ids(
        string_records,
        record_types={"string_interaction"},
    )
    enrichment_taxon_ids = _record_taxon_ids(
        enrichment_records,
        record_types={"david_enrichment", "functional_enrichment"},
    )
    mapping_taxon_ids = {
        mapping.taxon_id
        for mapping in string_mappings
        if mapping.taxon_id is not None
    }
    if len(string_taxon_ids) > 1:
        raise ValueError("STRING records contain multiple TaxIDs.")
    if len(enrichment_taxon_ids) > 1:
        raise ValueError("Enrichment records contain multiple TaxIDs.")
    if len(mapping_taxon_ids) > 1:
        raise ValueError("STRING identifier mappings contain multiple TaxIDs.")
    if (
        string_taxon_ids
        and enrichment_taxon_ids
        and string_taxon_ids != enrichment_taxon_ids
    ):
        raise ValueError(
            "STRING and enrichment records have different TaxIDs and cannot "
            "be merged into one evidence network."
        )
    record_taxon_ids = string_taxon_ids or enrichment_taxon_ids
    if (
        record_taxon_ids
        and mapping_taxon_ids
        and record_taxon_ids != mapping_taxon_ids
    ):
        raise ValueError(
            "STRING identifier mappings do not match the evidence record TaxID."
        )

    for record in string_records:
        if record.get("record_type") != "string_interaction":
            continue
        source_id = _required_text(record, "string_id_a")
        target_id = _required_text(record, "string_id_b")
        taxon_id = _positive_int_or_none(record.get("taxon_id"))
        source_label = str(record.get("preferred_name_a") or source_id)
        target_label = str(record.get("preferred_name_b") or target_id)
        nodes.setdefault(
            source_id,
            EvidenceNode(
                node_id=source_id,
                label=source_label,
                node_type="protein",
                taxon_id=taxon_id,
            ),
        )
        nodes.setdefault(
            target_id,
            EvidenceNode(
                node_id=target_id,
                label=target_label,
                node_type="protein",
                taxon_id=taxon_id,
            ),
        )
        trace = _trace_from(
            record,
            string_provenance,
            fallback_source_name="STRING",
        )
        combined_score = _score_or_none(record.get("combined_score"))
        pair = tuple(sorted((source_id, target_id)))
        if combined_score is not None:
            existing = rankings.get(pair)
            if existing is None or combined_score > existing.combined_score:
                rankings[pair] = RankedRelationship(
                    source_node_id=pair[0],
                    target_node_id=pair[1],
                    combined_score=combined_score,
                    trace=trace,
                )

        channel_found = False
        for field, (channel, evidence_type) in STRING_CHANNELS.items():
            score = _score_or_none(record.get(field))
            if score is None or score <= 0:
                continue
            channel_found = True
            edges.append(
                EvidenceEdge(
                    source_node_id=source_id,
                    target_node_id=target_id,
                    relationship=(
                        "physical_association"
                        if record.get("network_type") == "physical"
                        else "functional_association"
                    ),
                    evidence_type=evidence_type,
                    evidence_channel=channel,
                    channel_score=score,
                    ranking_score=combined_score,
                    trace=trace,
                )
            )
        if not channel_found and combined_score is not None:
            warnings.append(
                f"{source_id}—{target_id} has only a combined ranking score; "
                "no evidence edge was created."
            )

    enrichment = _enrichment_evidence(
        enrichment_records,
        provenance=enrichment_provenance,
    )
    for item in enrichment:
        node_id = f"term:{item.category}:{item.term_id}"
        nodes.setdefault(
            node_id,
            EvidenceNode(
                node_id=node_id,
                label=item.term_name,
                node_type="term",
                taxon_id=item.taxon_id,
            ),
        )
        for gene_id in item.gene_ids:
            gene_node_id = _input_node_id(gene_id)
            nodes.setdefault(
                gene_node_id,
                EvidenceNode(
                    node_id=gene_node_id,
                    label=gene_id,
                    node_type="gene",
                    taxon_id=item.taxon_id,
                ),
            )
            edges.append(
                EvidenceEdge(
                    source_node_id=gene_node_id,
                    target_node_id=node_id,
                    relationship="annotated_to_term",
                    evidence_type=EvidenceType.CURATED_DATABASE,
                    evidence_channel="david_annotation_membership",
                    channel_score=1.0,
                    trace=item.trace,
                )
            )

    enrichment_gene_ids = {
        gene_id
        for item in enrichment
        for gene_id in item.gene_ids
    }
    mapped_input_ids: set[str] = set()
    for mapping in string_mappings:
        canonical = mapping.canonical_identifier
        if mapping.ambiguous or not canonical:
            continue
        input_identifier = mapping.input_identifier
        input_node_id = _input_node_id(input_identifier)
        nodes.setdefault(
            input_node_id,
            EvidenceNode(
                node_id=input_node_id,
                label=input_identifier,
                node_type=(
                    "gene"
                    if input_identifier in enrichment_gene_ids
                    else "identifier"
                ),
                taxon_id=mapping.taxon_id,
            ),
        )
        candidate = next(
            (
                item
                for item in mapping.candidates
                if item.identifier == canonical
            ),
            None,
        )
        nodes.setdefault(
            canonical,
            EvidenceNode(
                node_id=canonical,
                label=(
                    candidate.label
                    if candidate and candidate.label
                    else canonical
                ),
                node_type="protein",
                taxon_id=(
                    candidate.taxon_id
                    if candidate and candidate.taxon_id
                    else mapping.taxon_id
                ),
            ),
        )
        edges.append(
            EvidenceEdge(
                source_node_id=input_node_id,
                target_node_id=canonical,
                relationship="maps_to_string_protein",
                evidence_type=EvidenceType.CURATED_DATABASE,
                evidence_channel="string_identifier_mapping",
                channel_score=1.0,
                trace=_trace_from(
                    {"source_url": "https://string-db.org"},
                    string_provenance,
                    fallback_source_name="STRING",
                ),
            )
        )
        mapped_input_ids.add(input_identifier)

    if string_records and enrichment:
        if not string_mappings:
            warnings.append(
                "STRING identifier mappings were not supplied, so STRING and "
                "DAVID evidence layers were not linked."
            )
        elif not mapped_input_ids.intersection(enrichment_gene_ids):
            warnings.append(
                "STRING mapping inputs and DAVID gene IDs share no exact "
                "identifier, so the evidence layers were not linked; no "
                "cross-database identity was guessed."
            )

    return EvidenceNetwork(
        nodes=tuple(sorted(nodes.values(), key=lambda item: item.node_id)),
        edges=tuple(
            sorted(
                edges,
                key=lambda item: (
                    item.source_node_id,
                    item.target_node_id,
                    item.evidence_type.value,
                    item.evidence_channel,
                ),
            )
        ),
        rankings=tuple(
            rankings[key]
            for key in sorted(rankings)
        ),
        enrichment=enrichment,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _enrichment_evidence(
    records: Sequence[Mapping[str, Any]],
    *,
    provenance: ProvenanceRecord | None,
) -> tuple[EnrichmentEvidence, ...]:
    usable = [
        record
        for record in records
        if record.get("record_type") in {
            "david_enrichment",
            "functional_enrichment",
        }
        and _score_or_none(record.get("p_value")) is not None
    ]
    output: list[EnrichmentEvidence] = []
    for record in usable:
        reported = _score_or_none(record.get("bh_adjusted_p_value"))
        taxon_id = _positive_int_or_none(record.get("taxon_id"))
        if taxon_id is None:
            raise ValueError(
                "Every enrichment record must contain a positive TaxID."
            )
        term_id = str(
            record.get("term_id")
            if record.get("term_id") is not None
            else record.get("term_name") or "unreported"
        )
        output.append(
            EnrichmentEvidence(
                term_id=term_id,
                term_name=str(record.get("term_name") or term_id),
                category=str(record.get("category") or "unreported"),
                gene_ids=tuple(
                    str(value)
                    for value in record.get("gene_ids") or ()
                ),
                hit_count=_non_negative_int_or_none(
                    record.get("hit_count")
                ),
                input_total=_non_negative_int_or_none(
                    record.get("list_total")
                    or record.get("input_total")
                ),
                background_hit_count=_non_negative_int_or_none(
                    record.get("background_hit_count")
                ),
                background_total=_non_negative_int_or_none(
                    record.get("background_total")
                ),
                p_value=float(record["p_value"]),
                bh_adjusted_p_value=reported,
                correction_source=(
                    "source_reported"
                    if reported is not None
                    else "not_reported"
                ),
                fold_enrichment=_non_negative_float_or_none(
                    record.get("fold_enrichment")
                ),
                taxon_id=taxon_id,
                trace=_trace_from(
                    record,
                    provenance,
                    fallback_source_name=(
                        "DAVID"
                        if record.get("record_type") == "david_enrichment"
                        else "functional enrichment"
                    ),
                ),
            )
        )
    return tuple(
        sorted(
            output,
            key=lambda item: (
                item.bh_adjusted_p_value is None,
                (
                    item.bh_adjusted_p_value
                    if item.bh_adjusted_p_value is not None
                    else 1.0
                ),
                item.p_value,
                item.category,
                item.term_id,
            ),
        )
    )


def _input_node_id(identifier: str) -> str:
    return f"input:{identifier}"


def _record_taxon_ids(
    records: Sequence[Mapping[str, Any]],
    *,
    record_types: set[str],
) -> set[int]:
    return {
        taxon_id
        for record in records
        if record.get("record_type") in record_types
        if (taxon_id := _positive_int_or_none(record.get("taxon_id")))
        is not None
    }


def _trace_from(
    record: Mapping[str, Any],
    provenance: ProvenanceRecord | None,
    *,
    fallback_source_name: str,
) -> SourceTrace:
    source_url = str(
        record.get("source_url")
        or (provenance.endpoint_url if provenance else "")
    )
    if not source_url:
        raise ValueError("Evidence records require a clickable source URL.")
    return SourceTrace(
        source_name=(
            provenance.source_name if provenance else fallback_source_name
        ),
        source_url=source_url,
        source_version=(
            provenance.source_version
            if provenance
            else (
                str(record.get("string_version"))
                if record.get("string_version")
                else str(record.get("knowledgebase_version") or "") or None
            )
        ),
        retrieved_at_utc=(
            provenance.retrieved_at_utc.isoformat()
            if provenance
            else None
        ),
        raw_response_sha256=(
            provenance.raw_response_sha256 if provenance else None
        ),
    )


def _required_text(record: Mapping[str, Any], field: str) -> str:
    value = str(record.get(field) or "").strip()
    if not value:
        raise ValueError(f"Evidence record is missing {field}.")
    return value


def _score_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    number = float(value)
    if number < 0 or number > 1:
        raise ValueError("Evidence scores must be between 0 and 1.")
    return number


def _non_negative_float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    number = float(value)
    if number < 0:
        raise ValueError("Value must be non-negative.")
    return number


def _positive_int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    number = int(value)
    if number < 1:
        raise ValueError("TaxID must be positive.")
    return number


def _non_negative_int_or_none(value: Any) -> int | None:
    if value in (None, ""):
        return None
    number = int(value)
    if number < 0:
        raise ValueError("Value must be non-negative.")
    return number


__all__ = [
    "EnrichmentEvidence",
    "EvidenceEdge",
    "EvidenceNetwork",
    "EvidenceNode",
    "EvidenceType",
    "RankedRelationship",
    "SourceTrace",
    "benjamini_hochberg",
    "build_evidence_network",
]
