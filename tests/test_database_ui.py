from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from streamlit.testing.v1 import AppTest

import vetevidence.connector_artifacts as connector_artifacts_module
import vetevidence.database_connectors as database_connectors_module
import vetevidence.run_store as run_store_module


APP_PATH = Path(__file__).parents[1] / "app.py"


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
    isolated_run_root = tmp_path / "runs"
    isolated_connector_root = tmp_path / "connectors"
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
    monkeypatch.delenv("NCBI_EMAIL", raising=False)
    monkeypatch.delenv("DAVID_EMAIL", raising=False)

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
