from __future__ import annotations

from datetime import UTC, datetime

import pytest

from vetevidence.database_connectors import (
    IdentifierCandidate,
    IdentifierMapping,
    ProvenanceRecord,
)
from vetevidence.evidence_network import (
    EvidenceEdge,
    EvidenceType,
    benjamini_hochberg,
    build_evidence_network,
)


def _provenance(source_name: str, endpoint: str) -> ProvenanceRecord:
    return ProvenanceRecord(
        source_name=source_name,
        endpoint_url=endpoint,
        method="POST",
        normalized_request='{"method":"POST"}',
        request_sha256="a" * 64,
        raw_response_sha256="b" * 64,
        retrieved_at_utc=datetime(2026, 7, 30, tzinfo=UTC),
        http_status=200,
        content_type="application/json",
        source_version="12.0" if source_name == "STRING" else None,
    )


def _string_record() -> dict[str, object]:
    return {
        "record_type": "string_interaction",
        "string_id_a": "9913.P11111",
        "string_id_b": "9913.P22222",
        "preferred_name_a": "Protein A",
        "preferred_name_b": "Protein B",
        "taxon_id": 9913,
        "network_type": "functional",
        "combined_score": 0.91,
        "combined_score_role": "ranking_only",
        "experimental_score": 0.8,
        "database_score": 0.4,
        "text_mining_score": 0.2,
        "neighborhood_score": 0.1,
        "fusion_score": 0.0,
        "phylogenetic_profile_score": 0.0,
        "coexpression_score": 0.5,
        "string_version": "12.0",
        "source_url": (
            "https://version-12-0.string-db.org/cgi/network?"
            "identifiers=9913.P11111%0d9913.P22222"
        ),
    }


def test_benjamini_hochberg_keeps_original_order() -> None:
    assert benjamini_hochberg([0.01, 0.04, 0.03, 0.002]) == pytest.approx(
        (0.02, 0.04, 0.04, 0.008)
    )
    assert benjamini_hochberg([]) == ()

    with pytest.raises(ValueError, match="between 0 and 1"):
        benjamini_hochberg([1.1])


def test_string_channels_become_distinct_evidence_types() -> None:
    network = build_evidence_network(
        [_string_record()],
        string_provenance=_provenance(
            "STRING",
            "https://version-12-0.string-db.org/api/json/network",
        ),
    )

    channels = {edge.evidence_channel: edge.evidence_type for edge in network.edges}
    assert channels == {
        "experimental": EvidenceType.EXPERIMENTAL,
        "database": EvidenceType.CURATED_DATABASE,
        "text_mining": EvidenceType.TEXT_MINED,
        "gene_neighborhood": EvidenceType.COMPUTATIONAL_PREDICTION,
        "coexpression": EvidenceType.COMPUTATIONAL_PREDICTION,
    }
    assert len(network.rankings) == 1
    assert network.rankings[0].combined_score == 0.91
    assert network.rankings[0].role == "ranking_only"
    assert all(edge.evidence_channel != "combined_score" for edge in network.edges)
    assert network.edges[0].trace.raw_response_sha256 == "b" * 64
    assert network.edges[0].trace.source_url.startswith("https://")


def test_combined_score_without_channel_is_not_promoted_to_evidence() -> None:
    record = _string_record()
    for field in (
        "experimental_score",
        "database_score",
        "text_mining_score",
        "neighborhood_score",
        "fusion_score",
        "phylogenetic_profile_score",
        "coexpression_score",
    ):
        record[field] = 0

    network = build_evidence_network([record])

    assert network.edges == ()
    assert len(network.rankings) == 1
    assert "no evidence edge" in network.warnings[0]


def test_combined_channel_is_rejected_by_model() -> None:
    with pytest.raises(ValueError, match="ranking-only"):
        EvidenceEdge(
            source_node_id="a",
            target_node_id="b",
            relationship="association",
            evidence_type=EvidenceType.COMPUTATIONAL_PREDICTION,
            evidence_channel="combined_score",
            channel_score=0.9,
            ranking_score=0.9,
            trace={
                "source_name": "STRING",
                "source_url": "https://string-db.org",
            },
        )


def test_enrichment_keeps_missing_bh_unreported_with_trace() -> None:
    enrichment = [
        {
            "record_type": "david_enrichment",
            "term_id": 10,
            "term_name": "Pathway A",
            "category": "KEGG_PATHWAY",
            "gene_ids": ["1", "2"],
            "hit_count": 2,
            "list_total": 2,
            "background_hit_count": 20,
            "background_total": 200,
            "p_value": 0.01,
            "bh_adjusted_p_value": 0.03,
            "fold_enrichment": 10,
            "taxon_id": 9913,
            "source_url": "https://davidbioinformatics.nih.gov/chartReport.html",
        },
        {
            "record_type": "david_enrichment",
            "term_id": 20,
            "term_name": "Pathway B",
            "category": "KEGG_PATHWAY",
            "gene_ids": ["2"],
            "hit_count": 1,
            "list_total": 2,
            "background_hit_count": 40,
            "background_total": 200,
            "p_value": 0.04,
            "bh_adjusted_p_value": None,
            "fold_enrichment": 2.5,
            "taxon_id": 9913,
            "source_url": "https://davidbioinformatics.nih.gov/chartReport.html",
        },
    ]

    network = build_evidence_network(
        [],
        enrichment_records=enrichment,
        enrichment_provenance=_provenance(
            "DAVID",
            "https://davidbioinformatics.nih.gov/webservice/",
        ),
    )

    by_term = {item.term_id: item for item in network.enrichment}
    assert by_term["10"].bh_adjusted_p_value == 0.03
    assert by_term["10"].correction_source == "source_reported"
    assert by_term["20"].bh_adjusted_p_value is None
    assert by_term["20"].correction_source == "not_reported"
    assert by_term["20"].trace.source_name == "DAVID"
    assert by_term["20"].trace.raw_response_sha256 == "b" * 64


def test_string_mapping_links_david_gene_membership_to_protein_layer() -> None:
    mapping = IdentifierMapping(
        input_identifier="1",
        namespace="protein_identifier",
        canonical_identifier="9913.P11111",
        candidates=(
            IdentifierCandidate(
                identifier="9913.P11111",
                label="Protein A",
                taxon_id=9913,
            ),
        ),
        mapping_method="string_best_match_with_taxon",
        taxon_id=9913,
    )
    network = build_evidence_network(
        [_string_record()],
        string_mappings=[mapping],
        string_provenance=_provenance(
            "STRING",
            "https://version-12-0.string-db.org/api/json/network",
        ),
        enrichment_records=[
            {
                "record_type": "david_enrichment",
                "term_id": "bta04010",
                "term_name": "MAPK signaling pathway",
                "category": "KEGG_PATHWAY",
                "gene_ids": ["1"],
                "p_value": 0.01,
                "bh_adjusted_p_value": 0.03,
                "taxon_id": 9913,
                "source_url": (
                    "https://davidbioinformatics.nih.gov/chartReport.html"
                ),
            }
        ],
        enrichment_provenance=_provenance(
            "DAVID",
            "https://davidbioinformatics.nih.gov/webservice/",
        ),
    )

    by_channel = {edge.evidence_channel: edge for edge in network.edges}
    assert by_channel["string_identifier_mapping"].source_node_id == "input:1"
    assert (
        by_channel["string_identifier_mapping"].target_node_id
        == "9913.P11111"
    )
    assert (
        by_channel["david_annotation_membership"].source_node_id
        == "input:1"
    )
    assert (
        by_channel["david_annotation_membership"].target_node_id
        == "term:KEGG_PATHWAY:bta04010"
    )
    assert not any("not linked" in warning for warning in network.warnings)


def test_unshared_cross_database_identifiers_are_not_guessed() -> None:
    mapping = IdentifierMapping(
        input_identifier="999",
        namespace="protein_identifier",
        canonical_identifier="9913.P11111",
        candidates=(
            IdentifierCandidate(
                identifier="9913.P11111",
                taxon_id=9913,
            ),
        ),
        mapping_method="string_best_match_with_taxon",
        taxon_id=9913,
    )
    network = build_evidence_network(
        [_string_record()],
        string_mappings=[mapping],
        enrichment_records=[
            {
                "record_type": "david_enrichment",
                "term_id": "bta04010",
                "term_name": "MAPK signaling pathway",
                "category": "KEGG_PATHWAY",
                "gene_ids": ["1"],
                "p_value": 0.01,
                "taxon_id": 9913,
                "source_url": "https://davidbioinformatics.nih.gov/",
            }
        ],
    )

    assert any("share no exact identifier" in item for item in network.warnings)
    assert all(
        not (
            edge.evidence_channel == "string_identifier_mapping"
            and edge.source_node_id == "input:1"
        )
        for edge in network.edges
    )


def test_evidence_network_rejects_mixed_taxids_within_string_layer() -> None:
    other_species = _string_record()
    other_species["string_id_a"] = "9606.P11111"
    other_species["string_id_b"] = "9606.P22222"
    other_species["taxon_id"] = 9606

    with pytest.raises(ValueError, match="STRING records contain multiple TaxIDs"):
        build_evidence_network([_string_record(), other_species])


def test_evidence_network_rejects_mixed_taxids_within_enrichment_layer() -> None:
    records = [
        {
            "record_type": "david_enrichment",
            "term_name": "Bovine pathway",
            "category": "KEGG_PATHWAY",
            "p_value": 0.01,
            "taxon_id": 9913,
        },
        {
            "record_type": "david_enrichment",
            "term_name": "Human pathway",
            "category": "KEGG_PATHWAY",
            "p_value": 0.02,
            "taxon_id": 9606,
        },
    ]

    with pytest.raises(
        ValueError,
        match="Enrichment records contain multiple TaxIDs",
    ):
        build_evidence_network([], enrichment_records=records)


def test_evidence_network_rejects_cross_layer_taxid_mismatch() -> None:
    with pytest.raises(ValueError, match="different TaxIDs"):
        build_evidence_network(
            [_string_record()],
            enrichment_records=[
                {
                    "record_type": "david_enrichment",
                    "term_name": "Human pathway",
                    "category": "KEGG_PATHWAY",
                    "p_value": 0.01,
                    "taxon_id": 9606,
                }
            ],
        )


def test_enrichment_requires_taxid_and_clickable_source() -> None:
    with pytest.raises(ValueError, match="positive TaxID"):
        build_evidence_network(
            [],
            enrichment_records=[
                {
                    "record_type": "david_enrichment",
                    "term_name": "Pathway",
                    "category": "KEGG_PATHWAY",
                    "p_value": 0.01,
                    "source_url": "https://example.test",
                }
            ],
        )
