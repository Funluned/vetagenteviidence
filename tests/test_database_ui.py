from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from streamlit.testing.v1 import AppTest

import vetevidence.connector_artifacts as connector_artifacts_module
import vetevidence.database_batch_artifacts as database_batch_artifacts_module
import vetevidence.database_connectors as database_connectors_module
import vetevidence.docking_ui as docking_ui_module
import vetevidence.run_store as run_store_module
from vetevidence.openbabel_execution import OpenBabelExecutionError
from vetevidence.vina_execution import VinaExecutionError


APP_PATH = Path(__file__).parents[1] / "app.py"


def _unavailable_vina(*_: Any, **__: Any) -> None:
    raise VinaExecutionError("test fixture: Vina unavailable")


def _unavailable_openbabel(*_: Any, **__: Any) -> None:
    raise OpenBabelExecutionError("test fixture: Open Babel unavailable")


def _element_with_key_prefix(
    elements: Iterable[Any],
    prefix: str,
) -> Any:
    matches = [
        element
        for element in elements
        if str(getattr(element, "key", "")).startswith(prefix)
    ]
    assert len(matches) == 1, (
        f"Expected exactly one element with key prefix {prefix!r}, "
        f"found {[getattr(element, 'key', None) for element in matches]!r}."
    )
    return matches[0]


def _has_key_prefix(elements: Iterable[Any], prefix: str) -> bool:
    return any(
        str(getattr(element, "key", "")).startswith(prefix)
        for element in elements
    )


def _create_research_task(
    tmp_path: Path,
    monkeypatch: Any,
) -> AppTest:
    real_run_store = run_store_module.RunStore
    real_connector_store = connector_artifacts_module.ConnectorArtifactStore
    real_batch_store = (
        database_batch_artifacts_module.DatabaseBatchArtifactStore
    )
    isolated_run_root = tmp_path / "runs"
    isolated_connector_root = tmp_path / "connectors"
    isolated_batch_root = tmp_path / "connector-batches"
    monkeypatch.setattr(
        run_store_module,
        "RunStore",
        lambda root=None: real_run_store(root or isolated_run_root),
    )
    monkeypatch.setattr(
        connector_artifacts_module,
        "ConnectorArtifactStore",
        lambda root=None: real_connector_store(
            root or isolated_connector_root
        ),
    )
    monkeypatch.setattr(
        database_batch_artifacts_module,
        "DatabaseBatchArtifactStore",
        lambda root=None, connector_store=None: real_batch_store(
            root or isolated_batch_root,
            connector_store=(
                connector_store
                or real_connector_store(isolated_connector_root)
            ),
        ),
    )
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    monkeypatch.delenv("DAVID_EMAIL", raising=False)
    monkeypatch.delenv("OMIM_API_KEY", raising=False)
    monkeypatch.delenv("DRUGBANK_API_KEY", raising=False)
    monkeypatch.setattr(
        docking_ui_module,
        "discover_vina",
        _unavailable_vina,
    )
    monkeypatch.setattr(
        docking_ui_module,
        "discover_openbabel",
        _unavailable_openbabel,
    )

    app = AppTest.from_file(str(APP_PATH)).run(timeout=40)
    assert not app.exception

    create_button = next(
        button
        for button in app.button
        if button.label == "创建或重置研究任务"
    )
    app = create_button.click().run(timeout=40)

    assert not app.exception
    assert len(list(isolated_run_root.glob("*.json"))) == 1
    return app


def test_database_ui_is_single_source_and_switches_rcsb_modes(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    app = _create_research_task(tmp_path, monkeypatch)

    source = _element_with_key_prefix(
        app.selectbox,
        "database-source-",
    )
    assert source.value == "PubChem 化合物"

    pubchem_query = _element_with_key_prefix(
        app.text_area,
        "database-query-pubchem-",
    )
    assert pubchem_query.label == "化合物名称、CID 或 InChIKey *"
    assert pubchem_query.value == ""
    assert _has_key_prefix(
        app.selectbox,
        "database-pubchem-namespace-",
    )
    assert _has_key_prefix(
        app.checkbox,
        "database-pubchem-3d-",
    )
    assert not _has_key_prefix(app.selectbox, "database-species-")
    assert not _has_key_prefix(app.text_input, "database-ncbi-email-")
    assert not _has_key_prefix(app.segmented_control, "database-gene-mode-")
    assert not _has_key_prefix(app.segmented_control, "database-rcsb-mode-")

    pubchem_submit = _element_with_key_prefix(
        app.button,
        "database-submit-pubchem-",
    )
    assert pubchem_submit.label == "开始联网检索"
    assert pubchem_submit.disabled is True

    app = source.select("NCBI Gene").run(timeout=40)
    assert not app.exception

    gene_query = _element_with_key_prefix(
        app.text_area,
        "database-query-ncbi-gene-",
    )
    assert gene_query.label == "基因符号 *"
    gene_mode = _element_with_key_prefix(
        app.segmented_control,
        "database-gene-mode-",
    )
    assert gene_mode.value == "基因符号"
    species = _element_with_key_prefix(
        app.selectbox,
        "database-species-ncbi-gene-",
    )
    assert species.label == "物种 *"
    assert species.value is None
    ncbi_email = _element_with_key_prefix(
        app.text_input,
        "database-ncbi-email-",
    )
    assert ncbi_email.label == "NCBI 联系邮箱（联网必填）"
    assert not _has_key_prefix(
        app.text_area,
        "database-query-pubchem-",
    )
    assert not _has_key_prefix(
        app.selectbox,
        "database-pubchem-namespace-",
    )
    assert not _has_key_prefix(
        app.checkbox,
        "database-pubchem-3d-",
    )

    source = _element_with_key_prefix(
        app.selectbox,
        "database-source-",
    )
    app = source.select("RCSB PDB").run(timeout=40)
    assert not app.exception

    rcsb_mode = _element_with_key_prefix(
        app.segmented_control,
        "database-rcsb-mode-",
    )
    assert rcsb_mode.value == "PDB ID"
    rcsb_query = _element_with_key_prefix(
        app.text_area,
        "database-query-rcsb-pdb-",
    )
    assert rcsb_query.label == "PDB ID *"
    assert _has_key_prefix(app.checkbox, "database-pdb-mmcif-")
    assert not _has_key_prefix(
        app.selectbox,
        "database-species-rcsb-pdb-",
    )

    app = rcsb_mode.select("按 UniProt 找结构").run(timeout=40)
    assert not app.exception

    rcsb_query = _element_with_key_prefix(
        app.text_area,
        "database-query-rcsb-pdb-",
    )
    assert rcsb_query.label == "UniProt accession *"
    species = _element_with_key_prefix(
        app.selectbox,
        "database-species-rcsb-pdb-",
    )
    assert species.label == "物种 *"
    assert species.value is None
    assert _has_key_prefix(
        app.checkbox,
        "database-rcsb-experimental-",
    )
    assert _has_key_prefix(
        app.number_input,
        "database-rcsb-limit-",
    )
    assert not _has_key_prefix(app.checkbox, "database-pdb-mmcif-")


def test_multi_database_batch_keeps_inputs_independent_and_archives_membership(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    pubchem_calls: list[str] = []

    class FakePubChemConnector:
        def __enter__(self) -> FakePubChemConnector:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def fetch_compound(
            self,
            identifier: str,
            **_: object,
        ) -> database_connectors_module.ConnectorResult:
            pubchem_calls.append(identifier)
            return database_connectors_module.ConnectorResult(
                status=database_connectors_module.ConnectorStatus.OK,
                records=(
                    {
                        "record_type": "compound",
                        "cid": identifier,
                        "source_url": "https://pubchem.example.test/",
                    },
                ),
            )

    monkeypatch.setattr(
        database_connectors_module,
        "PubChemConnector",
        FakePubChemConnector,
    )
    app = _create_research_task(tmp_path, monkeypatch)

    mode = _element_with_key_prefix(
        app.segmented_control,
        "database-mode-",
    )
    assert mode.value == "单库检索"
    app = mode.select("多库批量").run(timeout=40)
    sources = _element_with_key_prefix(
        app.multiselect,
        "database-sources-",
    )
    app = sources.set_value(
        ["PubChem 化合物", "OMIM 人类遗传"]
    ).run(timeout=40)
    assert not app.exception
    assert not _has_key_prefix(app.selectbox, "database-source-")

    pubchem_query = _element_with_key_prefix(
        app.text_area,
        "database-query-pubchem-",
    )
    omim_query = _element_with_key_prefix(
        app.text_area,
        "database-query-omim-",
    )
    pubchem_query.set_value("shared\nshared\naspirin")
    omim_query.set_value("shared")
    submit = _element_with_key_prefix(
        app.button,
        "database-submit-batch-",
    )
    app = submit.click().run(timeout=40)

    assert not app.exception
    assert pubchem_calls == ["shared", "aspirin"]
    state = app.session_state["database_connector_results"]
    batch = state["current_batch"]
    assert batch["batch_id"] == state["latest_batch_id"]
    assert batch["source_keys"] == ["pubchem", "omim"]
    assert batch["planned_count"] == 3
    assert batch["succeeded_count"] == 3
    assert batch["failed_count"] == 0
    assert batch["status"] == "complete"
    assert batch["archive_valid"] is True
    assert len(batch["query_ids"]) == 3
    assert len(list((tmp_path / "connectors").rglob("manifest.json"))) == 3
    batch_manifests = list(
        (tmp_path / "connector-batches").rglob("batch-manifest.json")
    )
    assert len(batch_manifests) == 1
    assert batch["batch_id"] in batch_manifests[0].parts

    snapshot_path = next((tmp_path / "runs").glob("*.json"))
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    calls = [
        call
        for call in snapshot["tool_calls"]
        if call["tool_name"] in {"database.pubchem", "database.omim"}
    ]
    assert [call["tool_name"] for call in calls] == [
        "database.pubchem",
        "database.pubchem",
        "database.omim",
    ]
    assert len({call["metadata"]["batch_id"] for call in calls}) == 1
    assert calls[-1]["metadata"]["connector_status"] == "offline_export"


def test_multi_database_runtime_failure_does_not_stop_next_source(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    uniprot_calls: list[tuple[str, int]] = []

    class FailingPubChemConnector:
        def __enter__(self) -> FailingPubChemConnector:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def fetch_compound(self, *_: object, **__: object) -> None:
            raise RuntimeError("fixture PubChem failure")

    class FakeUniProtConnector:
        def __enter__(self) -> FakeUniProtConnector:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def fetch_protein(
            self,
            accession: str,
            *,
            taxon_id: int,
        ) -> database_connectors_module.ConnectorResult:
            uniprot_calls.append((accession, taxon_id))
            return database_connectors_module.ConnectorResult(
                status=database_connectors_module.ConnectorStatus.OK,
                records=(
                    {
                        "record_type": "protein",
                        "primary_accession": accession,
                        "taxon_id": taxon_id,
                        "source_url": "https://uniprot.example.test/",
                    },
                ),
            )

    monkeypatch.setattr(
        database_connectors_module,
        "PubChemConnector",
        FailingPubChemConnector,
    )
    monkeypatch.setattr(
        database_connectors_module,
        "UniProtConnector",
        FakeUniProtConnector,
    )
    app = _create_research_task(tmp_path, monkeypatch)
    mode = _element_with_key_prefix(
        app.segmented_control,
        "database-mode-",
    )
    app = mode.select("多库批量").run(timeout=40)
    sources = _element_with_key_prefix(
        app.multiselect,
        "database-sources-",
    )
    app = sources.set_value(
        ["PubChem 化合物", "UniProt 蛋白"]
    ).run(timeout=40)

    _element_with_key_prefix(
        app.text_area,
        "database-query-pubchem-",
    ).set_value("bad")
    _element_with_key_prefix(
        app.text_area,
        "database-query-uniprot-",
    ).set_value("P00533")
    _element_with_key_prefix(
        app.selectbox,
        "database-species-uniprot-",
    ).select("人（Homo sapiens）")
    submit = _element_with_key_prefix(
        app.button,
        "database-submit-batch-",
    )
    app = submit.click().run(timeout=40)

    assert not app.exception
    assert uniprot_calls == [("P00533", 9606)]
    batch = app.session_state["database_connector_results"]["current_batch"]
    assert batch["planned_count"] == 2
    assert batch["succeeded_count"] == 1
    assert batch["failed_count"] == 1
    assert batch["status"] == "partial"
    assert batch["archive_valid"] is True
    assert {row["source"] for row in batch["operations"]} == {
        "PubChem",
        "UniProt",
    }
    assert any("fixture PubChem failure" in item.value for item in app.error)


def test_multi_database_operation_cap_blocks_all_connectors_before_execution(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    constructor_calls: list[str] = []

    class ForbiddenConnector:
        def __init__(self, *_: object, **__: object) -> None:
            constructor_calls.append("called")
            raise AssertionError("connector must not be initialized")

    monkeypatch.setattr(
        database_connectors_module,
        "PubChemConnector",
        ForbiddenConnector,
    )
    monkeypatch.setattr(
        database_connectors_module,
        "UniProtConnector",
        ForbiddenConnector,
    )
    monkeypatch.setattr(
        database_connectors_module,
        "NCBIConnector",
        ForbiddenConnector,
    )
    app = _create_research_task(tmp_path, monkeypatch)
    mode = _element_with_key_prefix(
        app.segmented_control,
        "database-mode-",
    )
    app = mode.select("多库批量").run(timeout=40)
    sources = _element_with_key_prefix(
        app.multiselect,
        "database-sources-",
    )
    app = sources.set_value(
        [
            "PubChem 化合物",
            "UniProt 蛋白",
            "NCBI Gene",
            "GenBank",
        ]
    ).run(timeout=40)

    _element_with_key_prefix(
        app.text_area,
        "database-query-pubchem-",
    ).set_value("\n".join(f"compound-{index}" for index in range(10)))
    _element_with_key_prefix(
        app.text_area,
        "database-query-uniprot-",
    ).set_value("\n".join(f"P{index:05d}" for index in range(20)))
    _element_with_key_prefix(
        app.text_area,
        "database-query-ncbi-gene-",
    ).set_value("\n".join(f"GENE{index}" for index in range(20)))
    _element_with_key_prefix(
        app.text_area,
        "database-query-genbank-",
    ).set_value("\n".join(f"NM_{index:06d}.1" for index in range(10)))
    for source_key in ("uniprot", "ncbi-gene", "genbank"):
        _element_with_key_prefix(
            app.selectbox,
            f"database-species-{source_key}-",
        ).select("人（Homo sapiens）")
    _element_with_key_prefix(
        app.text_input,
        "database-ncbi-email-",
    ).set_value("researcher@example.test")
    submit = _element_with_key_prefix(
        app.button,
        "database-submit-batch-",
    )
    app = submit.click().run(timeout=40)

    assert not app.exception
    assert constructor_calls == []
    assert any(
        "一次最多执行 50 个真实数据库操作" in item.value
        for item in app.error
    )
    assert not list((tmp_path / "connectors").rglob("manifest.json"))


def test_rcsb_uniprot_search_calls_connector_and_renders_result(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeRCSBConnector:
        def __enter__(self) -> FakeRCSBConnector:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def fetch_structure(self, *_: object, **__: object) -> None:
            raise AssertionError(
                "UniProt→PDB mode must not call fetch_structure."
            )

        def search_structures(
            self,
            accession: str,
            *,
            taxon_id: int,
            experimental_only: bool,
            max_results: int,
        ) -> database_connectors_module.ConnectorResult:
            calls.append(
                {
                    "accession": accession,
                    "taxon_id": taxon_id,
                    "experimental_only": experimental_only,
                    "max_results": max_results,
                }
            )
            return database_connectors_module.ConnectorResult(
                status=database_connectors_module.ConnectorStatus.OK,
                records=(
                    {
                        "record_type": "structure_hit",
                        "pdb_id": "1IVO",
                        "uniprot_accession": accession,
                        "taxon_id": taxon_id,
                        "source_url": "https://www.rcsb.org/structure/1IVO",
                    },
                ),
            )

    monkeypatch.setattr(
        database_connectors_module,
        "RCSBConnector",
        FakeRCSBConnector,
    )
    app = _create_research_task(tmp_path, monkeypatch)

    source = _element_with_key_prefix(
        app.selectbox,
        "database-source-",
    )
    app = source.select("RCSB PDB").run(timeout=40)
    assert not app.exception

    mode = _element_with_key_prefix(
        app.segmented_control,
        "database-rcsb-mode-",
    )
    app = mode.select("按 UniProt 找结构").run(timeout=40)
    assert not app.exception

    query = _element_with_key_prefix(
        app.text_area,
        "database-query-rcsb-pdb-",
    )
    app = query.set_value("P00533").run(timeout=40)
    assert not app.exception

    species = _element_with_key_prefix(
        app.selectbox,
        "database-species-rcsb-pdb-",
    )
    app = species.select("人（Homo sapiens）").run(timeout=40)
    assert not app.exception

    submit = _element_with_key_prefix(
        app.button,
        "database-submit-rcsb-pdb-",
    )
    assert submit.label == "开始联网检索"
    assert submit.disabled is False
    app = submit.click().run(timeout=40)

    assert not app.exception
    assert calls == [
        {
            "accession": "P00533",
            "taxon_id": 9606,
            "experimental_only": True,
            "max_results": 25,
        }
    ]
    assert any(
        "RCSB PDB 联网检索完成" in message.value
        for message in app.success
    )
    assert any(
        "1IVO" in str(table.value.to_dict())
        for table in app.dataframe
    )
    assert list((tmp_path / "connectors").rglob("manifest.json"))


def test_new_database_sources_show_offline_and_license_gates(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    app = _create_research_task(tmp_path, monkeypatch)

    source = _element_with_key_prefix(app.selectbox, "database-source-")
    app = source.select("OMIM 人类遗传").run(timeout=40)
    assert not app.exception
    omim_query = _element_with_key_prefix(
        app.text_area,
        "database-query-omim-",
    )
    assert omim_query.label == "MIM 编号、基因符号或疾病名称 *"
    assert not _has_key_prefix(app.selectbox, "database-species-omim-")
    omim_submit = _element_with_key_prefix(
        app.button,
        "database-submit-omim-",
    )
    assert omim_submit.label == "生成离线请求"
    assert omim_submit.disabled is True

    app = omim_query.set_value("100640").run(timeout=40)
    omim_submit = _element_with_key_prefix(
        app.button,
        "database-submit-omim-",
    )
    assert omim_submit.disabled is False
    app = omim_submit.click().run(timeout=40)
    assert not app.exception
    assert any(
        "OMIM 未向数据库发送请求" in message.value
        for message in app.warning
    )

    source = _element_with_key_prefix(app.selectbox, "database-source-")
    app = source.select("DrugBank 药物").run(timeout=40)
    assert not app.exception
    assert _has_key_prefix(app.checkbox, "database-license-drugbank-")
    drugbank_submit = _element_with_key_prefix(
        app.button,
        "database-submit-drugbank-",
    )
    assert drugbank_submit.label == "生成离线请求"
    assert not _has_key_prefix(app.selectbox, "database-species-drugbank-")
    drugbank_query = _element_with_key_prefix(
        app.text_area,
        "database-query-drugbank-",
    )
    app = drugbank_query.set_value("DB01050").run(timeout=40)
    drugbank_submit = _element_with_key_prefix(
        app.button,
        "database-submit-drugbank-",
    )
    assert drugbank_submit.disabled is False
    app = drugbank_submit.click().run(timeout=40)
    assert not app.exception
    snapshot_path = next((tmp_path / "runs").glob("*.json"))
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    drugbank_calls = [
        call
        for call in snapshot["tool_calls"]
        if call["tool_name"] == "database.drugbank"
    ]
    assert len(drugbank_calls) == 1
    assert "license_attestation=not_confirmed" in (
        drugbank_calls[0]["input_summary"]
    )
    assert "license_attestation=confirmed" not in (
        drugbank_calls[0]["input_summary"]
    )


def test_manual_database_imports_render_curated_and_prediction_labels(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    app = _create_research_task(tmp_path, monkeypatch)

    source = _element_with_key_prefix(app.selectbox, "database-source-")
    app = source.select("GeneCards 人类基因").run(timeout=40)
    assert not app.exception
    assert _has_key_prefix(app.text_input, "database-query-genecards-")
    license_checkbox = _element_with_key_prefix(
        app.checkbox,
        "database-license-genecards-",
    )
    app = license_checkbox.set_value(True).run(timeout=40)
    uploader = _element_with_key_prefix(
        app.file_uploader,
        "database-import-genecards-",
    )
    app = uploader.set_value(
        (
            "genecards.csv",
            (
                b"Gene Symbol,Description,Relevance Score\n"
                b"BRCA1,BRCA1 DNA repair associated,87.5\n"
            ),
            "text/csv",
        )
    ).run(timeout=40)
    submit = _element_with_key_prefix(
        app.button,
        "database-submit-genecards-",
    )
    assert submit.label == "导入授权文件"
    assert submit.disabled is False
    app = submit.click().run(timeout=40)
    assert not app.exception
    assert any(
        "GeneCards 文件导入完成" in message.value
        for message in app.success
    )

    source = _element_with_key_prefix(app.selectbox, "database-source-")
    app = source.select("SwissTargetPrediction 靶点预测").run(timeout=40)
    assert not app.exception
    smiles = _element_with_key_prefix(
        app.text_area,
        "database-query-swiss-target-prediction-",
    )
    app = smiles.set_value("CCO").run(timeout=40)
    species = _element_with_key_prefix(
        app.selectbox,
        "database-species-swiss-target-prediction-",
    )
    app = species.select("人（Homo sapiens）").run(timeout=40)
    confirmation = _element_with_key_prefix(
        app.checkbox,
        "database-import-confirm-swiss-target-prediction-",
    )
    app = confirmation.set_value(True).run(timeout=40)
    uploader = _element_with_key_prefix(
        app.file_uploader,
        "database-import-swiss-target-prediction-",
    )
    app = uploader.set_value(
        (
            "swiss-target.csv",
            (
                b"Target,Common Name,UniProt ID,Probability\n"
                b"Epidermal growth factor receptor,EGFR,P00533,0.87\n"
            ),
            "text/csv",
        )
    ).run(timeout=40)
    submit = _element_with_key_prefix(
        app.button,
        "database-submit-swiss-target-prediction-",
    )
    assert submit.label == "导入预测结果"
    assert submit.disabled is False
    app = submit.click().run(timeout=40)
    assert not app.exception
    assert any(
        "SwissTargetPrediction 文件导入完成" in message.value
        for message in app.success
    ), {
        "success": [message.value for message in app.success],
        "warning": [message.value for message in app.warning],
        "error": [message.value for message in app.error],
    }
    assert any(
        "证据层级：计算预测" in caption.value
        for caption in app.caption
    )
