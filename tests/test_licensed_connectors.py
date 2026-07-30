from __future__ import annotations

import json

import httpx
import pytest

from vetevidence.database_connectors import (
    AcquisitionMode,
    ConnectorStatus,
    DatabaseEvidenceClass,
)
from vetevidence.licensed_connectors import (
    DrugBankConnector,
    OMIMConnector,
)


def _unexpected_request(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"Unexpected external call to {request.url.host}")


def _omim_entry(
    mim_number: int = 100640,
    *,
    title: str = "ALDEHYDE DEHYDROGENASE 1 FAMILY MEMBER A1; ALDH1A1",
) -> dict[str, object]:
    return {
        "prefix": "*",
        "mimNumber": mim_number,
        "status": "live",
        "titles": {"preferredTitle": title},
        "geneMap": {
            "geneSymbols": "ALDH1A1, ALDH1",
            "phenotypeMapList": [
                {
                    "phenotypeMap": {
                        "mimNumber": mim_number,
                        "phenotype": "Test phenotype",
                        "phenotypeMimNumber": 600001,
                        "phenotypicSeriesNumber": "PS123456",
                        "phenotypeMappingKey": 3,
                        "phenotypeInheritance": "Autosomal dominant",
                    }
                }
            ],
        },
    }


def _drug_detail(
    drugbank_id: str = "DB00316",
    *,
    name: str = "Acetaminophen",
) -> dict[str, object]:
    return {
        "drugbank_id": drugbank_id,
        "name": name,
        "cas_number": "103-90-2",
        "annotation_status": "complete",
        "type": "Small Molecule",
        "groups": ["Approved", "Investigational"],
        "identifiers": {
            "drugbank_id": drugbank_id,
            "inchi": "InChI=1S/C8H9NO2",
            "inchikey": "RZVAJINKPMORJF-UHFFFAOYSA-N",
            "atc_codes": [{"code": "N02BE01", "title": "paracetamol"}],
        },
        "references": {
            "literature_references": [
                {
                    "ref_id": "A1",
                    "pubmed_id": 123456,
                    "citation": "A minimal test citation.",
                }
            ]
        },
    }


def _drug_bonds() -> list[dict[str, object]]:
    return [
        {
            "type": "Target",
            "bio_entity": {
                "bio_entity_id": "BE0000123",
                "name": "Test target",
                "organism": "Humans",
            },
            "known_action": "yes",
            "actions": ["inhibitor"],
            "inhibition_strength": None,
            "induction_strength": None,
            "references": {
                "literature_references": [
                    {
                        "ref_id": "A2",
                        "pubmed_id": 987654,
                        "citation": "A target interaction citation.",
                    }
                ]
            },
        }
    ]


def _bio_entity_details() -> list[dict[str, object]]:
    return [
        {
            "id": "BE0000123",
            "name": "Official test target",
            "kind": "protein",
            "organism": "Humans",
            "ncbi_taxonomy_id": "NCBI:txid9606",
            "description": None,
            "synonyms": None,
            "polypeptides": [
                {
                    "id": "P12345",
                    "uniprot_id": "P12345",
                    "uniprot_ids": ["P12345", "Q99999"],
                    "gene_name": "TARGET1",
                }
            ],
            "small_molecules": [],
            "sequences": [],
        }
    ]


def test_omim_without_key_is_deterministic_offline_and_sends_no_request() -> None:
    with httpx.Client(
        transport=httpx.MockTransport(_unexpected_request)
    ) as client:
        connector = OMIMConnector(client=client)
        first = connector.fetch("100640")
        second = connector.fetch("100640")

    assert connector.request_count == 0
    assert first == second
    assert first.status == ConnectorStatus.OFFLINE_EXPORT
    assert first.acquisition_mode == AcquisitionMode.OFFLINE_REQUEST
    assert first.evidence_class == DatabaseEvidenceClass.CURATED_DATABASE
    assert first.offline_request is not None
    payload = json.loads(first.offline_request.content)
    assert payload["parameters"]["mim_number"] == "100640"
    assert payload["required_configuration"] == ["OMIM_API_KEY"]
    assert "omim-secret-value" not in first.offline_request.content


def test_omim_entry_parses_gene_and_phenotype_and_redacts_key() -> None:
    api_key = "omim-secret-value"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/entry"
        assert request.url.params["mimNumber"] == "100640"
        assert request.url.params["apiKey"] == api_key
        return httpx.Response(
            200,
            json={
                "omim": {
                    "version": "1.0",
                    "entryList": [{"entry": _omim_entry()}],
                }
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = OMIMConnector(
            client=client,
            api_key=api_key,
            min_interval_seconds=0,
        ).fetch_entry("OMIM:100640")

    assert result.status == ConnectorStatus.OK
    record = result.records[0]
    assert record["record_type"] == "omim_entry"
    assert record["mim_number"] == "100640"
    assert record["gene_symbols"] == ["ALDH1A1", "ALDH1"]
    assert record["phenotype_mappings"][0] == {
        "gene_mim_number": "100640",
        "phenotype": "Test phenotype",
        "phenotype_mim_number": "600001",
        "phenotypic_series_number": "PS123456",
        "mapping_key": 3,
        "inheritance": "Autosomal dominant",
    }
    assert record["taxon_id"] == 9606
    assert result.provenance[0].source_version == "1.0"
    assert result.provenance[0].stable_ids == ("OMIM:100640",)
    normalized = result.provenance[0].normalized_request
    assert api_key not in normalized
    assert '"apiKey":"<redacted>"' in normalized


def test_omim_general_search_parses_documented_search_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/entry/search"
        assert request.url.params["search"] == "cardiomyopathy"
        assert request.url.params["limit"] == "2"
        return httpx.Response(
            200,
            json={
                "omim": {
                    "version": "1.0",
                    "searchResponse": {
                        "entryList": [
                            {"entry": _omim_entry(100640)},
                            {
                                "entry": _omim_entry(
                                    100641,
                                    title="SECOND TEST ENTRY",
                                )
                            },
                        ]
                    },
                }
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = OMIMConnector(
            client=client,
            api_key="authorized-key",
            min_interval_seconds=0,
        ).fetch("cardiomyopathy", max_results=2)

    assert result.status == ConnectorStatus.OK
    assert [record["mim_number"] for record in result.records] == [
        "100640",
        "100641",
    ]


@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (404, {"error": "not found"}, ConnectorStatus.NO_RESULTS),
        (503, {"error": "unavailable"}, ConnectorStatus.DEGRADED),
        (200, {"omim": {"entryList": []}}, ConnectorStatus.NO_RESULTS),
        (200, {"unexpected": []}, ConnectorStatus.DEGRADED),
    ],
)
def test_omim_reports_empty_http_and_schema_failures_honestly(
    status_code: int,
    body: dict[str, object],
    expected: ConnectorStatus,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = OMIMConnector(
            client=client,
            api_key="authorized-key",
            max_retries=0,
            min_interval_seconds=0,
        ).fetch_entry("100640")

    assert result.status == expected
    assert result.records == ()


def test_omim_invalid_json_is_degraded_not_an_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not-json", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = OMIMConnector(
            client=client,
            api_key="authorized-key",
            min_interval_seconds=0,
        ).fetch_entry("100640")

    assert result.status == ConnectorStatus.DEGRADED
    assert "schema" in result.warnings[0]


@pytest.mark.parametrize(
    ("api_key", "license_confirmed", "required"),
    [
        (
            None,
            False,
            ["DRUGBANK_API_KEY", "DRUGBANK_LICENSE_CONFIRMATION"],
        ),
        ("secret", False, ["DRUGBANK_LICENSE_CONFIRMATION"]),
        (None, True, ["DRUGBANK_API_KEY"]),
    ],
)
def test_drugbank_dual_gate_sends_no_request(
    api_key: str | None,
    license_confirmed: bool,
    required: list[str],
) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(_unexpected_request)
    ) as client:
        connector = DrugBankConnector(
            client=client,
            api_key=api_key,
            license_confirmed=license_confirmed,
        )
        result = connector.fetch_drug("DB00316")

    assert connector.request_count == 0
    assert result.status == ConnectorStatus.OFFLINE_EXPORT
    assert result.acquisition_mode == AcquisitionMode.OFFLINE_REQUEST
    assert result.offline_request is not None
    payload = json.loads(result.offline_request.content)
    assert payload["required_configuration"] == required
    assert "secret" not in result.offline_request.content


def test_drugbank_id_fetches_detail_and_bonds_with_minimal_fields() -> None:
    api_key = "drugbank-secret"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == api_key
        if request.url.path.endswith("/bonds"):
            assert request.url.params["per_page"] == "5"
            return httpx.Response(
                200,
                json=_drug_bonds(),
                headers={
                    "Link": '<https://example.test/bonds?page=1>; rel="self"',
                    "X-Total-Count": "1",
                    "X-Per-Page": "5",
                },
                request=request,
            )
        if request.url.path.endswith("/bio_entities"):
            assert request.url.params["ids"] == "BE0000123"
            return httpx.Response(
                200,
                json=_bio_entity_details(),
                request=request,
            )
        assert request.url.path.endswith("/drugs/DB00316")
        assert request.url.params["include_references"] == "true"
        return httpx.Response(200, json=_drug_detail(), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = DrugBankConnector(
            client=client,
            api_key=api_key,
            license_confirmed=True,
            min_interval_seconds=0,
        ).fetch_drug("db00316", max_results=5)

    assert result.status == ConnectorStatus.OK
    assert len(requests) == 3
    drug, bond = result.records
    assert drug["record_type"] == "drugbank_drug"
    assert drug["inchikey"] == "RZVAJINKPMORJF-UHFFFAOYSA-N"
    assert drug["references"][0]["pmid"] == "123456"
    assert bond["record_type"] == "drugbank_bond"
    assert bond["bio_entity_name"] == "Official test target"
    assert bond["uniprot_ids"] == ["P12345", "Q99999"]
    assert bond["gene_symbol"] == "TARGET1"
    assert bond["taxon_id"] == 9606
    assert bond["references"][0]["pmid"] == "987654"
    assert result.mappings[0].canonical_identifier == "DB00316"
    assert all(
        api_key not in provenance.normalized_request
        for provenance in result.provenance
    )
    assert all(
        "authorization" not in artifact.response_headers
        for artifact in result.artifacts
    )
    detail_artifact, bonds_artifact, bio_entities_artifact = result.artifacts
    assert detail_artifact.provenance.stable_ids == (
        "DrugBank:DB00316",
        "InChIKey:RZVAJINKPMORJF-UHFFFAOYSA-N",
    )
    assert bonds_artifact.provenance.stable_ids == (
        "DrugBankBioEntity:BE0000123",
    )
    assert bonds_artifact.response_headers == {
        "content-type": "application/json",
        "link": '<https://example.test/bonds?page=1>; rel="self"',
        "x-total-count": "1",
        "x-per-page": "5",
    }
    assert bio_entities_artifact.provenance.stable_ids == (
        "DrugBankBioEntity:BE0000123",
        "NCBITaxon:9606",
        "UniProtKB:P12345",
        "UniProtKB:Q99999",
    )


def test_drugbank_unique_name_search_resolves_then_fetches_detail() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/drugs"):
            assert request.url.params["q"] == 'name:"Acetaminophen"'
            return httpx.Response(
                200,
                json=[
                    {
                        "drugbank_id": "DB00316",
                        "name": "Acetaminophen",
                    }
                ],
                request=request,
            )
        return httpx.Response(
            200,
            json=_drug_detail(),
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = DrugBankConnector(
            client=client,
            api_key="authorized-key",
            license_confirmed=True,
            min_interval_seconds=0,
        ).fetch_drug("Acetaminophen", include_bonds=False)

    assert result.status == ConnectorStatus.OK
    assert paths == [
        "/discovery/v1/drugs",
        "/discovery/v1/drugs/DB00316",
    ]
    assert result.mappings[0].canonical_identifier == "DB00316"
    assert result.artifacts[0].provenance.stable_ids == (
        "DrugBank:DB00316",
    )
    assert result.artifacts[1].provenance.stable_ids == (
        "DrugBank:DB00316",
        "InChIKey:RZVAJINKPMORJF-UHFFFAOYSA-N",
    )


def test_drugbank_enriches_all_page_bio_entities_in_one_batch() -> None:
    bonds = _drug_bonds() + [
        {
            "type": "Enzyme",
            "bio_entity": {
                "bio_entity_id": "BE0000456",
                "name": "Second target",
                "organism": "Dogs",
            },
            "known_action": "unknown",
            "actions": ["substrate"],
            "inhibition_strength": None,
            "induction_strength": None,
        }
    ]
    details = _bio_entity_details() + [
        {
            "id": "BE0000456",
            "name": "Official second target",
            "kind": "protein",
            "organism": "Dogs",
            "ncbi_taxonomy_id": "NCBI:txid9615",
            "polypeptides": [
                {
                    "id": "A0A000",
                    "uniprot_id": "A0A000",
                    "uniprot_ids": ["A0A000"],
                    "gene_name": "DOG1",
                }
            ],
            "small_molecules": [],
            "sequences": [],
        }
    ]
    detail_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/bonds"):
            return httpx.Response(200, json=bonds, request=request)
        if request.url.path.endswith("/bio_entities"):
            detail_requests.append(request)
            assert request.url.params["ids"] == "BE0000123,BE0000456"
            return httpx.Response(200, json=details, request=request)
        return httpx.Response(200, json=_drug_detail(), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = DrugBankConnector(
            client=client,
            api_key="authorized-key",
            license_confirmed=True,
            min_interval_seconds=0,
        ).fetch_drug("DB00316", max_results=5)

    assert result.status == ConnectorStatus.OK
    assert len(detail_requests) == 1
    assert result.records[1]["uniprot_id"] == "P12345"
    assert result.records[2]["uniprot_id"] == "A0A000"
    assert result.records[2]["gene_symbol"] == "DOG1"
    assert result.records[2]["taxon_id"] == 9615


def test_drugbank_ambiguous_name_never_selects_first_result() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json=[
                {"drugbank_id": "DB00001", "name": "Compound alpha"},
                {"drugbank_id": "DB00002", "name": "Compound beta"},
            ],
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = DrugBankConnector(
            client=client,
            api_key="authorized-key",
            license_confirmed=True,
            min_interval_seconds=0,
        ).fetch_drug("Compound", include_bonds=False)

    assert result.status == ConnectorStatus.DEGRADED
    assert result.records == ()
    assert result.mappings[0].ambiguous is True
    assert [candidate.identifier for candidate in result.mappings[0].candidates] == [
        "DB00001",
        "DB00002",
    ]
    assert paths == ["/discovery/v1/drugs"]


def test_drugbank_unique_exact_name_is_not_confused_by_other_hits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/drugs"):
            return httpx.Response(
                200,
                json=[
                    {
                        "drugbank_id": "DB00316",
                        "name": "Acetaminophen",
                    },
                    {
                        "drugbank_id": "DB99999",
                        "name": "Acetaminophen mixture",
                    },
                ],
                request=request,
            )
        return httpx.Response(200, json=_drug_detail(), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = DrugBankConnector(
            client=client,
            api_key="authorized-key",
            license_confirmed=True,
            min_interval_seconds=0,
        ).fetch_drug("Acetaminophen", include_bonds=False)

    assert result.status == ConnectorStatus.OK
    assert result.records[0]["drugbank_id"] == "DB00316"


@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (404, {"error": "not found"}, ConnectorStatus.NO_RESULTS),
        (401, {"error": "unauthorized"}, ConnectorStatus.DEGRADED),
        (200, [], ConnectorStatus.NO_RESULTS),
        (200, {"unexpected": []}, ConnectorStatus.DEGRADED),
    ],
)
def test_drugbank_search_reports_empty_http_and_schema_failures(
    status_code: int,
    body: object,
    expected: ConnectorStatus,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = DrugBankConnector(
            client=client,
            api_key="authorized-key",
            license_confirmed=True,
            max_retries=0,
            min_interval_seconds=0,
        ).fetch_drug("unknown drug", include_bonds=False)

    assert result.status == expected
    assert result.records == ()


def test_drugbank_bond_failure_keeps_drug_but_marks_degraded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/bonds"):
            return httpx.Response(
                500,
                json={"error": "unavailable"},
                request=request,
            )
        return httpx.Response(200, json=_drug_detail(), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = DrugBankConnector(
            client=client,
            api_key="authorized-key",
            license_confirmed=True,
            max_retries=0,
            min_interval_seconds=0,
        ).fetch_drug("DB00316")

    assert result.status == ConnectorStatus.DEGRADED
    assert result.records[0]["record_type"] == "drugbank_drug"
    assert "HTTP 500" in result.warnings[0]


def test_drugbank_bio_entity_failure_keeps_unenriched_bonds() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/bonds"):
            return httpx.Response(200, json=_drug_bonds(), request=request)
        if request.url.path.endswith("/bio_entities"):
            return httpx.Response(
                503,
                json={
                    "error": "unavailable",
                    "uniprot_id": "MUST_NOT_BE_USED",
                },
                request=request,
            )
        return httpx.Response(200, json=_drug_detail(), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = DrugBankConnector(
            client=client,
            api_key="authorized-key",
            license_confirmed=True,
            max_retries=0,
            min_interval_seconds=0,
        ).fetch_drug("DB00316")

    assert result.status == ConnectorStatus.DEGRADED
    assert paths == [
        "/discovery/v1/drugs/DB00316",
        "/discovery/v1/drugs/DB00316/bonds",
        "/discovery/v1/bio_entities",
    ]
    bond = result.records[1]
    assert bond["bio_entity_id"] == "BE0000123"
    assert bond["taxon_id"] is None
    assert bond["uniprot_ids"] == []
    assert bond["uniprot_id"] is None
    assert bond["gene_symbols"] == []
    assert "HTTP 503" in result.warnings[-1]
    assert result.artifacts[-1].provenance.stable_ids == ()


def test_drugbank_truncated_bond_page_is_explicitly_degraded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/bonds"):
            return httpx.Response(
                200,
                json=_drug_bonds(),
                headers={
                    "Link": (
                        "<https://api.drugbank.com/discovery/v1/drugs/"
                        'DB00316/bonds?page=2>; rel="next"'
                    ),
                    "X-Total-Count": "8",
                    "X-Per-Page": "1",
                },
                request=request,
            )
        if request.url.path.endswith("/bio_entities"):
            return httpx.Response(
                200,
                json=_bio_entity_details(),
                request=request,
            )
        return httpx.Response(200, json=_drug_detail(), request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = DrugBankConnector(
            client=client,
            api_key="authorized-key",
            license_confirmed=True,
            min_interval_seconds=0,
        ).fetch_drug("DB00316", max_results=1)

    assert result.status == ConnectorStatus.DEGRADED
    assert len(result.records) == 2
    assert (
        "DrugBank returned only the first 1/8 bonds; additional pages "
        "were not retrieved."
    ) in result.warnings
    assert result.artifacts[1].response_headers["x-total-count"] == "8"
    assert 'rel="next"' in result.artifacts[1].response_headers["link"]


def test_drugbank_invalid_detail_json_is_degraded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = DrugBankConnector(
            client=client,
            api_key="authorized-key",
            license_confirmed=True,
            min_interval_seconds=0,
        ).fetch_drug("DB00316", include_bonds=False)

    assert result.status == ConnectorStatus.DEGRADED
    assert result.records == ()
    assert "detail schema" in result.warnings[0]
