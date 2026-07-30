from __future__ import annotations

import hashlib
import json
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from openpyxl import Workbook

import vetevidence.restricted_database_imports as restricted_imports_module
from vetevidence.database_connectors import (
    AcquisitionMode,
    ConnectorStatus,
    DatabaseEvidenceClass,
)
from vetevidence.restricted_database_imports import (
    MAX_IMPORT_FILE_BYTES,
    import_restricted_database_file,
)


def _xlsx(headers: list[str], rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(headers)
    for row in rows:
        worksheet.append(row)
    payload = BytesIO()
    workbook.save(payload)
    workbook.close()
    return payload.getvalue()


def _replace_xlsx_member(
    payload: bytes,
    member_name: str,
    replacement: bytes,
) -> bytes:
    output = BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(
        output,
        "w",
        compression=ZIP_DEFLATED,
    ) as target:
        for member in source.infolist():
            target.writestr(
                member.filename,
                (
                    replacement
                    if member.filename == member_name
                    else source.read(member.filename)
                ),
            )
    return output.getvalue()


def test_genecards_csv_normalizes_aliases_xrefs_and_provenance() -> None:
    payload = (
        "Gene Symbol,Description,Category,Relevance Score,GIFtS,"
        "HGNC ID,Entrez Gene ID,Ensembl Gene ID,UniProt ID,OMIM ID\n"
        "BRCA1,BRCA1 DNA repair associated,protein-coding,87.5,58,"
        "HGNC:1100,672,ENSG00000012048,P38398,113705\n"
    ).encode()

    result = import_restricted_database_file(
        "GeneCards",
        r"C:\private\genecards.csv",
        payload,
        license_confirmed=True,
        query_context={
            "query": "BRCA1",
            "api_key": "must-not-be-recorded",
            "email": "private@example.test",
        },
        source_version="GeneCards 6",
    )

    assert result.status == ConnectorStatus.OK
    assert result.acquisition_mode == AcquisitionMode.MANUAL_IMPORT
    assert result.evidence_class == DatabaseEvidenceClass.CURATED_DATABASE
    assert len(result.records) == 1
    record = result.records[0]
    assert record == {
        "record_type": "genecards_gene",
        "symbol": "BRCA1",
        "name": "BRCA1 DNA repair associated",
        "gene_type": "protein-coding",
        "relevance_score": 87.5,
        "knowledge_score": 58.0,
        "xrefs": {
            "hgnc": ["HGNC:1100"],
            "entrez_gene": ["672"],
            "ensembl_gene": ["ENSG00000012048"],
            "uniprot": ["P38398"],
            "omim": ["113705"],
        },
        "taxon_id": 9606,
        "source_authenticity": "user_attested_licensed_export",
        "source_url": (
            "https://www.genecards.org/cgi-bin/carddisp.pl?gene=BRCA1"
        ),
    }
    artifact = result.artifacts[0]
    provenance = artifact.provenance
    assert artifact.raw_response == payload
    assert provenance.method == "IMPORT"
    assert provenance.http_status is None
    assert provenance.raw_response_sha256 == hashlib.sha256(payload).hexdigest()
    assert provenance.request_sha256 == hashlib.sha256(
        provenance.normalized_request.encode()
    ).hexdigest()
    assert provenance.source_version == "GeneCards 6"
    assert provenance.stable_ids == (
        "NCBITaxon:9606",
        "GeneCards:BRCA1",
        "HGNC:1100",
        "NCBIGene:672",
        "Ensembl:ENSG00000012048",
        "UniProtKB:P38398",
        "OMIM:113705",
    )
    request = json.loads(provenance.normalized_request)
    assert request == {
        "byte_count": len(payload),
        "filename": "genecards.csv",
        "query_context": {"query": "BRCA1"},
        "sha256": hashlib.sha256(payload).hexdigest(),
        "source_attestation": {
            "confirmed": True,
            "terms_url": "https://www.lifemapsc.com/terms-of-use/",
            "type": "user_attested_licensed_export",
        },
    }
    assert "must-not-be-recorded" not in provenance.normalized_request
    assert "private@example.test" not in provenance.normalized_request
    assert "license_confirmed" not in provenance.normalized_request


def test_malacards_tsv_is_human_curated_evidence() -> None:
    payload = (
        "MalaCards ID\tDisease Name\tDisease Family\tMIFTS Score\t"
        "Relevance\tOMIM ID\tORPHA ID\tUMLS CUI\tICD-10\tMeSH ID\n"
        "breast_cancer\tBreast cancer\tCancer\t81.2\t0.73\t"
        "114480;600048\t145\tC0006142\tC50\tD001943\n"
    ).encode()

    result = import_restricted_database_file(
        "mala_cards",
        "malacards.tsv",
        payload,
        license_confirmed=True,
    )

    assert result.status == ConnectorStatus.OK
    assert result.evidence_class == DatabaseEvidenceClass.CURATED_DATABASE
    assert result.records[0] == {
        "record_type": "malacards_disease",
        "mcid": "breast_cancer",
        "disease_name": "Breast cancer",
        "family": "Cancer",
        "mifts_score": 81.2,
        "relevance_score": 0.73,
        "xrefs": {
            "omim": ["114480", "600048"],
            "orphanet": ["145"],
            "umls": ["C0006142"],
            "icd10": ["C50"],
            "mesh": ["D001943"],
        },
        "taxon_id": 9606,
        "source_authenticity": "user_attested_licensed_export",
        "source_url": "https://www.malacards.org/card/breast_cancer",
    }
    assert result.artifacts[0].provenance.stable_ids == (
        "NCBITaxon:9606",
        "MalaCards:breast_cancer",
        "OMIM:114480",
        "OMIM:600048",
        "Orphanet:145",
        "UMLS:C0006142",
        "ICD10:C50",
        "MeSH:D001943",
    )


def test_genecards_common_genealacart_columns_are_combined() -> None:
    payload = (
        "Gene Symbol,GeneCards Inferred Functionality Score (GIFtS),"
        "GeneCards ID,UniProtKB/Swiss-Prot ID,UniProtKB/TrEMBL ID\n"
        "TP53,62,GC17M007661,P04637,Q53GA5 Q9H3D4\n"
    ).encode()

    result = import_restricted_database_file(
        "genecards",
        "genealacart.csv",
        payload,
        license_confirmed=True,
    )

    record = result.records[0]
    assert record["knowledge_score"] == 62
    assert record["xrefs"]["genecards"] == ["GC17M007661"]
    assert record["xrefs"]["uniprot"] == ["P04637", "Q53GA5", "Q9H3D4"]


def test_swiss_xlsx_is_prediction_and_requires_explicit_query_context() -> None:
    payload = _xlsx(
        [
            "Target",
            "Common Name",
            "UniProt ID",
            "ChEMBL ID",
            "Target Class",
            "Probability*",
            "Known Actives (3D/2D)",
        ],
        [
            [
                "Epidermal growth factor receptor",
                "EGFR ERBB1",
                "P00533 Q9UE56",
                "CHEMBL203",
                "Kinase",
                0.87,
                "12 / 8",
            ]
        ],
    )

    result = import_restricted_database_file(
        "SwissTargetPrediction",
        "prediction.xlsx",
        payload,
        license_confirmed=True,
        query_context={
            "smiles": "CCO",
            "taxon_id": 9606,
            "inchikey": "LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
            "token": "secret",
        },
        source_version="2026 web export",
    )

    assert result.status == ConnectorStatus.OK
    assert result.acquisition_mode == AcquisitionMode.MANUAL_IMPORT
    assert (
        result.evidence_class
        == DatabaseEvidenceClass.COMPUTATIONAL_PREDICTION
    )
    assert result.records[0] == {
        "record_type": "swiss_target_prediction",
        "target_name": "Epidermal growth factor receptor",
        "gene_names": ["EGFR", "ERBB1"],
        "uniprot_ids": ["P00533", "Q9UE56"],
        "chembl_ids": ["CHEMBL203"],
        "target_class": "Kinase",
        "probability": 0.87,
        "known_actives": "12/8",
        "query_smiles": "CCO",
        "taxon_id": 9606,
        "source_authenticity": "user_attested_manual_prediction",
        "source_url": "https://www.swisstargetprediction.ch/",
    }
    provenance = result.artifacts[0].provenance
    assert provenance.stable_ids == (
        "NCBITaxon:9606",
        "InChIKey:LFQSCWFLJHTTHZ-UHFFFAOYSA-N",
        "UniProtKB:P00533",
        "UniProtKB:Q9UE56",
        "ChEMBL:CHEMBL203",
    )
    assert "secret" not in provenance.normalized_request


@pytest.mark.parametrize("source_key", ["GeneCards", "MalaCards"])
def test_licensed_sources_refuse_import_without_confirmation(
    source_key: str,
) -> None:
    with pytest.raises(ValueError, match="license confirmation"):
        import_restricted_database_file(
            source_key,
            "data.csv",
            b"symbol\n",
            license_confirmed=False,
        )


def test_swiss_refuses_import_without_manual_use_confirmation() -> None:
    with pytest.raises(ValueError, match="generated manually"):
        import_restricted_database_file(
            "SwissTargetPrediction",
            "result.csv",
            b"Target,Probability\nEGFR,0.8\n",
            license_confirmed=False,
            query_context={"smiles": "CCO", "taxon_id": 9606},
        )


def test_human_only_import_rejects_conflicting_taxon_context() -> None:
    with pytest.raises(ValueError, match="9606"):
        import_restricted_database_file(
            "GeneCards",
            "data.csv",
            b"Gene Symbol\nBRCA1\n",
            license_confirmed=True,
            query_context={"taxon_id": 10090},
        )


@pytest.mark.parametrize(
    ("query_context", "message"),
    [
        (None, "smiles"),
        ({"smiles": "", "taxon_id": 9606}, "smiles"),
        ({"smiles": "CCO"}, "taxon_id"),
        ({"smiles": "CCO", "taxon_id": 9913}, "9606"),
    ],
)
def test_swiss_requires_supported_taxon_and_smiles(
    query_context: dict[str, object] | None,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        import_restricted_database_file(
            "SwissTargetPrediction",
            "result.csv",
            b"Target,Probability\nEGFR,0.8\n",
            license_confirmed=True,
            query_context=query_context,
        )


@pytest.mark.parametrize("probability", ["NaN", "inf", "-0.01", "1.01"])
def test_swiss_rejects_nonfinite_and_out_of_range_probability(
    probability: str,
) -> None:
    with pytest.raises(ValueError, match="probability"):
        import_restricted_database_file(
            "SwissTargetPrediction",
            "result.csv",
            f"Target,Probability\nEGFR,{probability}\n".encode(),
            license_confirmed=True,
            query_context={"smiles": "CCO", "taxon_id": 10090},
        )


def test_header_only_file_returns_no_results_and_archives_raw_file() -> None:
    payload = b"Gene Symbol,Description\n"

    result = import_restricted_database_file(
        "GeneCards",
        "header-only.csv",
        payload,
        license_confirmed=True,
    )

    assert result.status == ConnectorStatus.NO_RESULTS
    assert result.records == ()
    assert result.artifacts[0].raw_response == payload
    assert result.artifacts[0].provenance.raw_response_sha256 == hashlib.sha256(
        payload
    ).hexdigest()


@pytest.mark.parametrize(
    ("filename", "payload", "message"),
    [
        ("empty.csv", b"", "empty"),
        ("unsupported.json", b"{}", "Only CSV"),
        ("blank.csv", b"\n", "header"),
        ("nul.csv", b"Gene Symbol\x00\nBRCA1\n", "NUL"),
    ],
)
def test_empty_and_invalid_files_are_rejected(
    filename: str,
    payload: bytes,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        import_restricted_database_file(
            "GeneCards",
            filename,
            payload,
            license_confirmed=True,
        )


def test_file_larger_than_ten_mib_is_rejected_before_parsing() -> None:
    payload = b"x" * (MAX_IMPORT_FILE_BYTES + 1)

    with pytest.raises(ValueError, match="10 MiB"):
        import_restricted_database_file(
            "GeneCards",
            "large.csv",
            payload,
            license_confirmed=True,
        )


@pytest.mark.parametrize(
    "headers",
    [
        ["Gene Symbol", "Gene Symbol"],
        ["Gene Symbol", "Symbol"],
        ["Gene Symbol", ""],
    ],
)
def test_duplicate_ambiguous_and_blank_headers_are_rejected(
    headers: list[str],
) -> None:
    payload = (",".join(headers) + "\nBRCA1,value\n").encode()

    with pytest.raises(ValueError, match="duplicate|ambiguously|empty"):
        import_restricted_database_file(
            "GeneCards",
            "ambiguous.csv",
            payload,
            license_confirmed=True,
        )


def test_missing_required_header_is_rejected() -> None:
    with pytest.raises(ValueError, match="missing required columns"):
        import_restricted_database_file(
            "MalaCards",
            "missing.csv",
            b"Disease Name\nBreast cancer\n",
            license_confirmed=True,
        )


def test_xlsx_formula_is_rejected_without_evaluation() -> None:
    payload = _xlsx(
        ["Target", "Probability"],
        [["EGFR", "=1/2"]],
    )

    with pytest.raises(ValueError, match="formulas"):
        import_restricted_database_file(
            "SwissTargetPrediction",
            "formula.xlsx",
            payload,
            license_confirmed=True,
            query_context={"smiles": "CCO", "taxon_id": 10116},
        )


def test_xlsx_import_fails_closed_without_defusedxml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _xlsx(["Gene Symbol"], [["BRCA1"]])
    monkeypatch.setattr(
        restricted_imports_module,
        "OPENPYXL_DEFUSEDXML_ENABLED",
        False,
    )

    with pytest.raises(RuntimeError, match="defusedxml"):
        import_restricted_database_file(
            "GeneCards",
            "unsafe-runtime.xlsx",
            payload,
            license_confirmed=True,
        )


def test_xlsx_xml_entities_are_rejected() -> None:
    assert restricted_imports_module.OPENPYXL_DEFUSEDXML_ENABLED is True
    payload = _replace_xlsx_member(
        _xlsx(["Gene Symbol"], [["BRCA1"]]),
        "xl/worksheets/sheet1.xml",
        b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE worksheet [<!ENTITY cell "BRCA1">]>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
    <row r="1"><c r="A1" t="inlineStr"><is><t>&cell;</t></is></c></row>
  </sheetData>
</worksheet>
""",
    )

    with pytest.raises(ValueError, match="parsed safely"):
        import_restricted_database_file(
            "GeneCards",
            "entity.xlsx",
            payload,
            license_confirmed=True,
        )


def test_xlsx_zip_bomb_ratio_is_rejected() -> None:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/worksheets/sheet1.xml", b"0" * (5 * 1024 * 1024))

    with pytest.raises(ValueError, match="compression ratio"):
        import_restricted_database_file(
            "GeneCards",
            "bomb.xlsx",
            output.getvalue(),
            license_confirmed=True,
        )


def test_only_supported_sources_are_accepted() -> None:
    with pytest.raises(ValueError, match="GeneCards"):
        import_restricted_database_file(
            "DrugBank",
            "drugbank.csv",
            b"DrugBank ID\nDB00001\n",
            license_confirmed=True,
        )
