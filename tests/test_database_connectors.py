from __future__ import annotations

import hashlib
import json
from urllib.parse import parse_qs

import httpx
import pytest

from vetevidence.database_connectors import (
    AcquisitionMode,
    CONNECTOR_PARSER_VERSION,
    ConnectorResult,
    ConnectorStatus,
    DAVIDConnector,
    NCBIConnector,
    PubChemConnector,
    RCSBConnector,
    RequestExecutor,
    STRINGConnector,
    UniProtConnector,
    export_connector_result,
    normalized_request,
)


def test_normalized_request_is_stable_and_redacts_credentials() -> None:
    first = normalized_request(
        "GET",
        "https://example.test",
        params={"z": 2, "api_key": "secret", "email": "user@example.test"},
    )
    second = normalized_request(
        "GET",
        "https://example.test",
        params={"email": "different", "api_key": "other", "z": 2},
    )

    assert first == second
    assert "secret" not in first
    assert "user@example.test" not in first
    assert first.count("<redacted>") == 2


def test_executor_retries_rate_limits_and_hashes_raw_response() -> None:
    attempts = 0
    clock = [0.0]
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, content=b'{"ok":true}', request=request)

    def monotonic() -> float:
        return clock[0]

    def sleep(seconds: float) -> None:
        if seconds:
            sleeps.append(seconds)
            clock[0] += seconds

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        executor = RequestExecutor(
            client,
            max_retries=1,
            retry_backoff_seconds=0,
            min_interval_seconds=1,
            sleep=sleep,
            monotonic=monotonic,
        )
        artifact = executor.request(
            "test",
            "GET",
            "https://example.test/data",
        )

    assert attempts == 2
    assert sleeps == [1.0]
    assert artifact.provenance.raw_response_sha256 == hashlib.sha256(
        b'{"ok":true}'
    ).hexdigest()
    assert len(artifact.provenance.request_sha256) == 64


def test_pubchem_ambiguous_name_requires_explicit_selection() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200,
            json={"IdentifierList": {"CID": [1, 2]}},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = PubChemConnector(
            client=client,
            min_interval_seconds=0,
        ).fetch_compound("ambiguous name")

    assert result.status == ConnectorStatus.DEGRADED
    assert result.records == ()
    assert result.mappings[0].ambiguous is True
    assert [item.identifier for item in result.mappings[0].candidates] == [
        "PubChem:CID:1",
        "PubChem:CID:2",
    ]
    assert request_count == 1


def test_pubchem_result_has_structure_ids_dates_and_provenance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cids/JSON"):
            return httpx.Response(
                200,
                json={"IdentifierList": {"CID": [2244]}},
                request=request,
            )
        if "/property/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "PropertyTable": {
                        "Properties": [
                            {
                                "CID": 2244,
                                "IUPACName": "2-acetyloxybenzoic acid",
                                "SMILES": "C[C@H](O)C(=O)O",
                                "ConnectivitySMILES": "CC(O)C(=O)O",
                                "InChIKey": "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
                                "MolecularFormula": "C9H8O4",
                                "MolecularWeight": "180.16",
                            }
                        ]
                    }
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "InformationList": {
                    "Information": [
                        {
                            "CID": 2244,
                            "CreateDate": "2004-09-16",
                            "ModifyDate": "2026-07-01",
                        }
                    ]
                }
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = PubChemConnector(
            client=client,
            min_interval_seconds=0,
        ).fetch_compound(
            "BSYNRYMUTXBXSQ-UHFFFAOYSA-N",
            namespace="inchikey",
        )

    assert result.status == ConnectorStatus.OK
    assert result.records[0]["cid"] == 2244
    assert result.records[0]["canonical_smiles"] == "CC(O)C(=O)O"
    assert result.records[0]["isomeric_smiles"] == "C[C@H](O)C(=O)O"
    assert result.records[0]["create_date"] == "2004-09-16"
    assert result.records[0]["modify_date"] == "2026-07-01"
    assert "InChIKey:BSYNRYMUTXBXSQ-UHFFFAOYSA-N" in (
        result.provenance[-1].stable_ids
    )
    assert (
        result.provenance[-1].source_version
        == "record-modified:2026-07-01"
    )
    assert result.provenance[-1].source_release_date is None


def test_pubchem_parses_current_nested_creation_date() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cids/JSON"):
            return httpx.Response(
                200,
                json={"IdentifierList": {"CID": [5280343]}},
                request=request,
            )
        if request.url.path.endswith("/dates/JSON"):
            return httpx.Response(
                200,
                json={
                    "InformationList": {
                        "Information": [
                            {
                                "CID": 5280343,
                                "CreationDate": {
                                    "Year": 2004,
                                    "Month": 9,
                                    "Day": 16,
                                },
                            }
                        ]
                    }
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "PropertyTable": {
                    "Properties": [
                        {
                            "CID": 5280343,
                            "InChIKey": "REFJWTPEDVJJIY-UHFFFAOYSA-N",
                        }
                    ]
                }
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = PubChemConnector(
            client=client,
            min_interval_seconds=0,
        ).fetch_compound("quercetin")

    assert result.records[0]["create_date"] == "2004-09-16"
    assert result.records[0]["modify_date"] is None
    assert result.provenance[-1].source_version is None


def test_pubchem_accepts_legacy_smiles_response_keys() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/property/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "PropertyTable": {
                        "Properties": [
                            {
                                "CID": 2244,
                                "CanonicalSMILES": "CC(O)C(=O)O",
                                "IsomericSMILES": "C[C@H](O)C(=O)O",
                            }
                        ]
                    }
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={"InformationList": {"Information": [{"CID": 2244}]}},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = PubChemConnector(
            client=client,
            min_interval_seconds=0,
        ).fetch_compound("2244", namespace="cid")

    assert result.records[0]["canonical_smiles"] == "CC(O)C(=O)O"
    assert result.records[0]["isomeric_smiles"] == "C[C@H](O)C(=O)O"


def test_uniprot_preserves_release_header_and_rejects_wrong_taxon() -> None:
    payload = {
        "primaryAccession": "P04637",
        "entryType": "UniProtKB reviewed (Swiss-Prot)",
        "proteinDescription": {
            "recommendedName": {"fullName": {"value": "Cellular tumor antigen p53"}}
        },
        "genes": [{"geneName": {"value": "TP53"}}],
        "organism": {"taxonId": 9606, "scientificName": "Homo sapiens"},
        "entryAudit": {
            "entryVersion": 200,
            "sequenceVersion": 4,
            "lastAnnotationUpdateDate": "2026-06-01",
        },
        "sequence": {"value": "MEEPQ", "length": 5},
        "uniProtKBCrossReferences": [
            {"database": "PDB", "id": "1TUP"},
            {"database": "GeneID", "id": "7157"},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=payload,
            headers={
                "x-uniprot-release": "2026_03",
                "x-uniprot-release-date": "24-June-2026",
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        connector = UniProtConnector(client=client)
        accepted = connector.fetch_protein("P04637", taxon_id=9606)
        rejected = connector.fetch_protein("P04637", taxon_id=9913)

    assert accepted.status == ConnectorStatus.OK
    assert accepted.records[0]["reviewed"] is True
    assert accepted.provenance[-1].source_version == "2026_03"
    assert accepted.provenance[-1].source_release_date == "24-June-2026"
    assert rejected.status == ConnectorStatus.DEGRADED
    assert rejected.records == ()
    assert "TaxID mismatch" in rejected.warnings[-1]


def test_ncbi_gene_symbol_ambiguity_and_genbank_version() -> None:
    def gene_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"esearchresult": {"idlist": ["1", "2"]}},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(gene_handler)) as client:
        result = NCBIConnector(
            client=client,
            email="researcher@example.test",
            min_interval_seconds=0,
        ).fetch_gene("ABC", taxon_id=9913, identifier_type="symbol")

    assert result.status == ConnectorStatus.DEGRADED
    assert result.mappings[0].ambiguous is True

    flatfile = b"""LOCUS       NC_000001 100 bp DNA
ACCESSION   NC_000001
VERSION     NC_000001.12
  ORGANISM  Test organism
            /db_xref="taxon:9913"
ORIGIN
//
"""

    def nucleotide_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=flatfile, request=request)

    with httpx.Client(
        transport=httpx.MockTransport(nucleotide_handler)
    ) as client:
        nucleotide = NCBIConnector(
            client=client,
            email="researcher@example.test",
            min_interval_seconds=0,
        ).fetch_nucleotide("NC_000001.12", taxon_id=9913)

    assert nucleotide.status == ConnectorStatus.OK
    assert nucleotide.records[0]["accession_version"] == "NC_000001.12"
    assert nucleotide.records[0]["taxon_id"] == 9913
    assert nucleotide.provenance[0].source_version == "NC_000001.12"


def test_ncbi_without_contact_email_does_not_send_request() -> None:
    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected external call to {request.url}")

    with httpx.Client(
        transport=httpx.MockTransport(fail_if_called)
    ) as client:
        connector = NCBIConnector(client=client, email=None)
        gene = connector.fetch_gene("101", taxon_id=9913)
        nucleotide = connector.fetch_nucleotide(
            "NC_000001.12",
            taxon_id=9913,
        )

    assert gene.status == ConnectorStatus.OFFLINE_EXPORT
    assert nucleotide.status == ConnectorStatus.OFFLINE_EXPORT
    assert gene.acquisition_mode is AcquisitionMode.OFFLINE_REQUEST
    assert nucleotide.acquisition_mode is AcquisitionMode.OFFLINE_REQUEST
    assert gene.offline_request is not None
    assert nucleotide.offline_request is not None
    assert "NCBI_EMAIL" in gene.offline_request.content


def test_rcsb_search_uses_searchable_taxonomy_lineage_attribute() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        taxon_query = payload["query"]["nodes"][1]["parameters"]
        assert taxon_query == {
            "attribute": "rcsb_entity_source_organism.taxonomy_lineage.id",
            "operator": "in",
            "value": ["9606"],
        }
        return httpx.Response(
            200,
            json={"result_set": [{"identifier": "1IVO"}]},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = RCSBConnector(client=client).search_structures(
            "P00533",
            taxon_id=9606,
            max_results=1,
        )

    assert result.status == ConnectorStatus.OK
    assert result.records[0]["pdb_id"] == "1IVO"


def test_rcsb_structure_keeps_label_and_author_chain_ids() -> None:
    entry_payload = {
        "struct": {"title": "Test receptor"},
        "exptl": [{"method": "X-RAY DIFFRACTION"}],
        "rcsb_entry_info": {"resolution_combined": [1.8]},
        "rcsb_accession_info": {
            "initial_release_date": "2020-01-01",
            "revision_date": "2026-01-01",
        },
        "rcsb_entry_container_identifiers": {
            "polymer_entity_ids": ["1"],
            "assembly_ids": ["1"],
        },
    }
    entity_payload = {
        "entity_poly": {"type": "polypeptide(L)"},
        "rcsb_polymer_entity": {"pdbx_description": "Target protein"},
        "rcsb_polymer_entity_container_identifiers": {
            "asym_ids": ["A"],
            "auth_asym_ids": ["R"],
            "reference_sequence_identifiers": [
                {
                    "database_name": "UniProt",
                    "database_accession": "P12345",
                }
            ],
        },
        "rcsb_entity_source_organism": [{"ncbi_taxonomy_id": 9913}],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "/polymer_entity/" in request.url.path:
            return httpx.Response(200, json=entity_payload, request=request)
        if request.url.path.endswith(".cif"):
            return httpx.Response(200, content=b"data_1ABC\n", request=request)
        return httpx.Response(200, json=entry_payload, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = RCSBConnector(client=client).fetch_structure("1abc")

    entity = result.records[0]["entities"][0]
    assert entity["label_asym_ids"] == ["A"]
    assert entity["auth_asym_ids"] == ["R"]
    assert result.records[0]["coordinate_mmcif_sha256"] == hashlib.sha256(
        b"data_1ABC\n"
    ).hexdigest()
    assert result.provenance[-1].source_version == "2026-01-01"


def test_string_uses_pinned_version_and_preserves_score_channels() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/version"):
            assert request.url.params["caller_identity"] == "VetEvidence-Agent"
            return httpx.Response(
                200,
                json=[
                    {
                        "string_version": "12.0",
                        "stable_address": (
                            "https://version-12-0.string-db.org"
                        ),
                    }
                ],
                request=request,
            )
        if request.url.path.endswith("/get_string_ids"):
            form = parse_qs(request.content.decode())
            assert form["species"] == ["9913"]
            return httpx.Response(
                200,
                json=[
                    {
                        "queryItem": "P11111",
                        "stringId": "9913.P11111",
                        "preferredName": "Protein A",
                        "ncbiTaxonId": 9913,
                    },
                    {
                        "queryItem": "P22222",
                        "stringId": "9913.P22222",
                        "preferredName": "Protein B",
                        "ncbiTaxonId": 9913,
                    },
                ],
                request=request,
            )
        assert request.url.host == "version-12-0.string-db.org"
        form = parse_qs(request.content.decode())
        assert form["add_nodes"] == ["0"]
        return httpx.Response(
            200,
            json=[
                {
                    "stringId_A": "9913.P11111",
                    "stringId_B": "9913.P22222",
                    "preferredName_A": "Protein A",
                    "preferredName_B": "Protein B",
                    "ncbiTaxonId": 9913,
                    "score": 0.91,
                    "escore": 0.8,
                    "dscore": 0.4,
                    "tscore": 0.2,
                    "nscore": 0.1,
                    "fscore": 0,
                    "pscore": 0,
                    "ascore": 0.5,
                }
            ],
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = STRINGConnector(
            client=client,
            min_interval_seconds=0,
        ).fetch_network(
            ["P11111", "P22222"],
            taxon_id=9913,
            consent_external_submission=True,
        )

    assert result.status == ConnectorStatus.OK
    assert result.records[0]["combined_score_role"] == "ranking_only"
    assert result.records[0]["experimental_score"] == 0.8
    assert result.records[0]["database_score"] == 0.4
    assert result.records[0]["text_mining_score"] == 0.2
    assert result.provenance[-1].source_version == "12.0"
    assert len(requests) == 3


def test_string_without_external_consent_only_exports_request() -> None:
    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected external call to {request.url}")

    with httpx.Client(
        transport=httpx.MockTransport(fail_if_called)
    ) as client:
        result = STRINGConnector(client=client).fetch_network(
            ["P11111", "P22222"],
            taxon_id=9913,
            consent_external_submission=False,
        )

    assert result.status == ConnectorStatus.OFFLINE_EXPORT
    assert result.acquisition_mode is AcquisitionMode.OFFLINE_REQUEST
    assert result.offline_request is not None
    request = json.loads(result.offline_request.content)
    assert request["taxon_id"] == 9913
    assert request["identifiers"] == ["P11111", "P22222"]


def test_david_requires_consent_background_and_registered_email() -> None:
    def fail_if_called(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected external call to {request.url}")

    with httpx.Client(
        transport=httpx.MockTransport(fail_if_called)
    ) as client:
        connector = DAVIDConnector(client=client)
        offline = connector.enrich(
            ["1"],
            taxon_id=9913,
            background=["1", "2"],
            consent_external_submission=False,
        )
        missing_credential = connector.enrich(
            ["1"],
            taxon_id=9913,
            background=["1", "2"],
            consent_external_submission=True,
        )

    assert offline.status == ConnectorStatus.OFFLINE_EXPORT
    assert offline.acquisition_mode is AcquisitionMode.OFFLINE_REQUEST
    assert offline.offline_request is not None
    assert json.loads(offline.offline_request.content)["taxon_id"] == 9913
    assert missing_credential.status == ConnectorStatus.DEGRADED
    assert (
        missing_credential.acquisition_mode
        is AcquisitionMode.OFFLINE_REQUEST
    )
    assert "registered organization email" in missing_credential.warnings[0]

    with pytest.raises(ValueError, match="background"):
        DAVIDConnector().enrich(
            ["1"],
            taxon_id=9913,
            background=[],
            consent_external_submission=False,
        )


def test_david_soap_parses_bh_adjusted_results() -> None:
    operations: list[str] = []

    def soap_response(operation: str, body: str) -> bytes:
        return (
            '<?xml version="1.0"?>'
            '<soapenv:Envelope '
            'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
            f"<soapenv:Body><{operation}Response>{body}"
            f"</{operation}Response></soapenv:Body></soapenv:Envelope>"
        ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        operation = request.headers["soapaction"].removeprefix("urn:")
        operations.append(operation)
        if operation == "authenticate":
            payload = soap_response(operation, "<return>true</return>")
        elif operation == "addList":
            payload = soap_response(operation, "<return>0.99</return>")
        elif operation == "getSpecies":
            payload = soap_response(
                operation,
                "<return>Bos taurus(2),Homo sapiens(1)</return>",
            )
        elif operation == "setCurrentSpecies":
            assert "<sam:args0>0</sam:args0>" in request.content.decode()
            assert "9913" not in request.content.decode()
            payload = soap_response(operation, "<return>0</return>")
        elif operation == "getCurrentSpecies":
            payload = soap_response(operation, "<return>0</return>")
        elif operation == "setCategories":
            payload = soap_response(
                operation,
                (
                    "<return>GOTERM_BP_DIRECT,GOTERM_CC_DIRECT,"
                    "GOTERM_MF_DIRECT,KEGG_PATHWAY,REACTOME_PATHWAY</return>"
                ),
            )
        elif operation == "getChartReport":
            payload = soap_response(
                operation,
                """
                <return>
                  <id>10</id>
                  <categoryName>KEGG_PATHWAY</categoryName>
                  <termName>bta04010~MAPK signaling pathway</termName>
                  <geneIds>1,2</geneIds>
                  <listHits>2</listHits>
                  <listTotals>2</listTotals>
                  <popHits>20</popHits>
                  <popTotals>200</popTotals>
                  <ease>0.01</ease>
                  <benjamini>0.03</benjamini>
                  <foldEnrichment>10.0</foldEnrichment>
                </return>
                """,
            )
        else:
            raise AssertionError(f"Unexpected DAVID operation: {operation}")
        return httpx.Response(
            200,
            content=payload,
            headers={"content-type": "text/xml"},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = DAVIDConnector(
            client=client,
            registered_email="lab@example.test",
            knowledgebase_version="v2026_1",
            min_interval_seconds=0,
        ).enrich(
            ["1", "2"],
            taxon_id=9913,
            background=["1", "2", "3"],
            consent_external_submission=True,
        )

    assert result.status == ConnectorStatus.DEGRADED
    assert result.records[0]["term_id"] == "bta04010"
    assert result.records[0]["term_name"] == "MAPK signaling pathway"
    assert result.records[0]["david_record_id"] == 10
    assert result.records[0]["p_value"] == 0.01
    assert result.records[0]["bh_adjusted_p_value"] == 0.03
    assert result.records[0]["background_total"] == 200
    assert result.records[0]["target_mapping_fraction"] == 0.99
    assert result.records[0]["background_mapping_fraction"] == 0.99
    assert (
        result.records[0]["user_asserted_knowledgebase_version"]
        == "v2026_1"
    )
    assert result.provenance[-1].source_version is None
    assert "user-asserted" in result.warnings[0]
    assert operations.index("getSpecies") < operations.index("setCurrentSpecies")
    assert operations.index("setCurrentSpecies") < operations.index(
        "getCurrentSpecies"
    )
    assert operations.index("getCurrentSpecies") < operations.index(
        "getChartReport"
    )


def test_david_stops_when_taxid_cannot_map_to_species_index() -> None:
    operations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        operation = request.headers["soapaction"].removeprefix("urn:")
        operations.append(operation)
        response_value = {
            "authenticate": "true",
            "addList": "0.99",
            "getSpecies": "Homo sapiens(2)",
        }[operation]
        payload = (
            '<?xml version="1.0"?>'
            '<soapenv:Envelope '
            'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
            f"<soapenv:Body><{operation}Response>"
            f"<return>{response_value}</return>"
            f"</{operation}Response></soapenv:Body></soapenv:Envelope>"
        ).encode()
        return httpx.Response(200, content=payload, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = DAVIDConnector(
            client=client,
            registered_email="lab@example.test",
            min_interval_seconds=0,
        ).enrich(
            ["1"],
            taxon_id=9913,
            background=["1", "2"],
            consent_external_submission=True,
        )

    assert result.status == ConnectorStatus.DEGRADED
    assert result.offline_request is not None
    assert "reliably map" in result.warnings[0]
    assert operations == ["authenticate", "addList", "getSpecies"]


def test_david_stops_when_categories_are_not_fully_confirmed() -> None:
    operations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        operation = request.headers["soapaction"].removeprefix("urn:")
        operations.append(operation)
        response_value = {
            "authenticate": "true",
            "addList": "0.99",
            "getSpecies": "Bos taurus(2)",
            "setCurrentSpecies": "0",
            "getCurrentSpecies": "0",
            "setCategories": "GOTERM_BP_DIRECT,KEGG_PATHWAY",
        }[operation]
        payload = (
            '<?xml version="1.0"?>'
            '<soapenv:Envelope '
            'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
            f"<soapenv:Body><{operation}Response>"
            f"<return>{response_value}</return>"
            f"</{operation}Response></soapenv:Body></soapenv:Envelope>"
        ).encode()
        return httpx.Response(200, content=payload, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = DAVIDConnector(
            client=client,
            registered_email="lab@example.test",
            min_interval_seconds=0,
        ).enrich(
            ["1"],
            taxon_id=9913,
            background=["1", "2"],
            consent_external_submission=True,
        )

    assert result.status == ConnectorStatus.DEGRADED
    assert "exactly the requested" in result.warnings[0]
    assert "getChartReport" not in operations


def test_result_export_is_deterministic_and_keeps_hash_not_raw_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"IdentifierList": {"CID": [1, 2]}},
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = PubChemConnector(
            client=client,
            min_interval_seconds=0,
        ).fetch_compound("ambiguous")

    first = export_connector_result(result)
    second = export_connector_result(result)
    assert first == second
    payload = json.loads(first.content)
    assert len(payload["artifacts"][0]["provenance"]["raw_response_sha256"]) == 64
    assert "raw_response" not in payload["artifacts"][0]
    assert payload["export_metadata"]["schema_version"].endswith("-v2")
    assert (
        payload["export_metadata"]["parser_version"]
        == CONNECTOR_PARSER_VERSION
        == "vetevidence-database-connectors-0.5"
    )
    assert len(payload["export_metadata"]["records_sha256"]) == 64
    assert payload["export_metadata"]["record_sha256"] == []
    assert (
        payload["export_metadata"]["raw_response_storage"]
        == "external_connector_artifacts"
    )


def test_result_export_hashes_each_normalized_record() -> None:
    result = ConnectorResult(
        status=ConnectorStatus.OK,
        records=(
            {"record_type": "gene", "gene_id": "1"},
            {"record_type": "gene", "gene_id": "2"},
        ),
    )

    payload = json.loads(export_connector_result(result).content)

    assert len(payload["export_metadata"]["record_sha256"]) == 2
    assert all(
        len(value) == 64
        for value in payload["export_metadata"]["record_sha256"]
    )
    assert len(payload["export_metadata"]["records_sha256"]) == 64
