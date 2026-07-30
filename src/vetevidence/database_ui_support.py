"""Pure helpers for the mixed-access database Streamlit page.

The module contains no Streamlit calls.  It keeps source metadata, veterinary
TaxID parsing, status summaries and hash-verified archive restoration
independently testable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from vetevidence.connector_artifacts import (
    ConnectorArchiveError,
    ConnectorArchiveManifest,
    ConnectorArtifactStore,
)
from vetevidence.database_connectors import (
    CONNECTOR_EXPORT_SCHEMA_VERSION,
    ConnectorResult,
    ConnectorStatus,
    DatabaseEvidenceClass,
    ResponseArtifact,
    canonical_json,
    sha256_bytes,
)


DatabaseSourceKey = Literal[
    "pubchem",
    "uniprot",
    "ncbi-gene",
    "genbank",
    "rcsb-pdb",
    "string",
    "david",
    "omim",
    "drugbank",
    "genecards",
    "malacards",
    "swiss-target-prediction",
]
DatabaseSourceAccessMode = Literal[
    "online_api",
    "credentialed_api",
    "licensed_api",
    "licensed_import",
    "manual_prediction_import",
]

CUSTOM_TAXON_LABEL = "自定义 TaxID"
MAX_RESTORED_CONNECTOR_ENTRIES = 100
DAVID_SUPPORTED_TAXON_IDS = frozenset(
    {9031, 9606, 9615, 9685, 9823, 9913, 10090, 10116}
)
SWISS_TARGET_SUPPORTED_TAXON_IDS = frozenset({9606, 10090, 10116})
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_RESULT_BYTES = 64 * 1024 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_RESPONSE_BYTES = 128 * 1024 * 1024
_SAFE_ARCHIVE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_RESPONSE_FILENAME = re.compile(
    r"^response-[0-9]{3}\.(?:json|xml|cif|sdf|csv|xlsx|txt|bin)$"
)
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "email",
        "key",
        "password",
        "token",
    }
)
_SAFE_REDACTED_VALUES = frozenset(
    {
        "",
        "<redacted>",
        "[redacted]",
        "[已脱敏]",
        "ncbi_email",
        "david_email",
        "${ncbi_email}",
        "${david_email}",
    }
)


class DatabaseUISupportModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class DatabaseSourceConfig(DatabaseUISupportModel):
    key: DatabaseSourceKey
    label: str = Field(min_length=1)
    input_label: str = Field(min_length=1)
    placeholder: str = Field(min_length=1)
    access_mode: DatabaseSourceAccessMode = "online_api"
    evidence_class: DatabaseEvidenceClass = (
        DatabaseEvidenceClass.CURATED_DATABASE
    )
    fixed_taxon_id: int | None = Field(default=None, ge=1)
    requires_taxon_id: bool = False
    requires_ncbi_email: bool = False
    requires_david_email: bool = False
    requires_omim_key: bool = False
    requires_drugbank_key: bool = False
    requires_license_confirmation: bool = False
    requires_manual_import: bool = False
    requires_external_consent: bool = False


DATABASE_SOURCE_CONFIGS: tuple[DatabaseSourceConfig, ...] = (
    DatabaseSourceConfig(
        key="pubchem",
        label="PubChem 化合物",
        input_label="化合物名称、CID 或 InChIKey",
        placeholder="例如：quercetin 或 5280343",
    ),
    DatabaseSourceConfig(
        key="uniprot",
        label="UniProt 蛋白",
        input_label="UniProt accession",
        placeholder="例如：P00533",
        requires_taxon_id=True,
    ),
    DatabaseSourceConfig(
        key="ncbi-gene",
        label="NCBI Gene",
        input_label="GeneID 或基因符号",
        placeholder="例如：3043 或 HBB",
        requires_taxon_id=True,
        requires_ncbi_email=True,
    ),
    DatabaseSourceConfig(
        key="genbank",
        label="GenBank",
        input_label="GenBank accession.version",
        placeholder="例如：NM_000518.5",
        requires_taxon_id=True,
        requires_ncbi_email=True,
    ),
    DatabaseSourceConfig(
        key="rcsb-pdb",
        label="RCSB PDB",
        input_label="PDB ID",
        placeholder="例如：1IEP",
    ),
    DatabaseSourceConfig(
        key="string",
        label="STRING 蛋白互作",
        input_label="蛋白 accession、基因名或 STRING ID",
        placeholder="例如：P00533；每行一个标识符",
        requires_taxon_id=True,
        requires_external_consent=True,
    ),
    DatabaseSourceConfig(
        key="david",
        label="DAVID 通路富集",
        input_label="目标基因标识符",
        placeholder="例如：3043；每行一个标识符",
        requires_taxon_id=True,
        requires_david_email=True,
        requires_external_consent=True,
    ),
    DatabaseSourceConfig(
        key="omim",
        label="OMIM 人类遗传",
        input_label="MIM 编号、基因符号或疾病名称",
        placeholder="例如：100100、BRCA1 或 Alzheimer disease",
        access_mode="credentialed_api",
        fixed_taxon_id=9606,
        requires_omim_key=True,
    ),
    DatabaseSourceConfig(
        key="drugbank",
        label="DrugBank 药物",
        input_label="DrugBank ID 或药物名称",
        placeholder="例如：DB01050 或 ibuprofen",
        access_mode="licensed_api",
        requires_drugbank_key=True,
        requires_license_confirmation=True,
    ),
    DatabaseSourceConfig(
        key="genecards",
        label="GeneCards 人类基因",
        input_label="GeneCards 授权导出文件",
        placeholder="上传 GeneALaCart 官方导出的 CSV 或 XLSX 文件",
        access_mode="licensed_import",
        fixed_taxon_id=9606,
        requires_license_confirmation=True,
        requires_manual_import=True,
    ),
    DatabaseSourceConfig(
        key="malacards",
        label="MalaCards 人类疾病",
        input_label="MalaCards 授权导出文件",
        placeholder="上传 MalaCards 官方导出的 CSV 或 XLSX 文件",
        access_mode="licensed_import",
        fixed_taxon_id=9606,
        requires_license_confirmation=True,
        requires_manual_import=True,
    ),
    DatabaseSourceConfig(
        key="swiss-target-prediction",
        label="SwissTargetPrediction 靶点预测",
        input_label="SwissTargetPrediction 预测结果文件",
        placeholder="上传官网下载的 CSV 或 XLSX 文件",
        access_mode="manual_prediction_import",
        evidence_class=DatabaseEvidenceClass.COMPUTATIONAL_PREDICTION,
        requires_taxon_id=True,
        requires_manual_import=True,
    ),
)

DATABASE_SOURCE_CONFIG_BY_KEY: Mapping[
    DatabaseSourceKey,
    DatabaseSourceConfig,
] = MappingProxyType({item.key: item for item in DATABASE_SOURCE_CONFIGS})

VETERINARY_SPECIES_TAX_IDS: Mapping[str, int] = MappingProxyType(
    {
        "牛（Bos taurus）": 9913,
        "犬（Canis lupus familiaris）": 9615,
        "猫（Felis catus）": 9685,
        "猪（Sus scrofa）": 9823,
        "马（Equus caballus）": 9796,
        "绵羊（Ovis aries）": 9940,
        "山羊（Capra hircus）": 9925,
        "鸡（Gallus gallus）": 9031,
        "兔（Oryctolagus cuniculus）": 9986,
        "小鼠（Mus musculus）": 10090,
        "大鼠（Rattus norvegicus）": 10116,
        "人（Homo sapiens）": 9606,
        "无乳链球菌（Streptococcus agalactiae）": 1311,
    }
)


class ConnectorResultSummary(DatabaseUISupportModel):
    total: int = Field(ge=0)
    online_available: int = Field(ge=0)
    no_results: int = Field(ge=0)
    offline_export: int = Field(ge=0)
    degraded: int = Field(ge=0)


class ConnectorArchiveEntry(DatabaseUISupportModel):
    source: str = Field(min_length=1)
    query_id: str = Field(min_length=1)
    created_at: datetime
    archive_path: str = Field(min_length=1)
    result: ConnectorResult


class ArchiveRestoreReport(DatabaseUISupportModel):
    entries: tuple[ConnectorArchiveEntry, ...] = ()
    warnings: tuple[str, ...] = ()


def parse_taxon_selection(
    value: str,
    custom_taxon_id: int | str | None = None,
) -> int:
    """Resolve one friendly species choice to a positive NCBI TaxID."""

    selection = value.strip()
    if selection in VETERINARY_SPECIES_TAX_IDS:
        return VETERINARY_SPECIES_TAX_IDS[selection]
    if selection != CUSTOM_TAXON_LABEL:
        raise ValueError("请选择列表中的物种或“自定义 TaxID”。")

    if custom_taxon_id is None or (
        isinstance(custom_taxon_id, str) and not custom_taxon_id.strip()
    ):
        raise ValueError("选择“自定义 TaxID”后必须填写 TaxID。")
    if isinstance(custom_taxon_id, bool):
        raise ValueError("TaxID 必须是正整数。")
    try:
        parsed = int(custom_taxon_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("TaxID 必须是正整数。") from exc
    if isinstance(custom_taxon_id, str) and str(parsed) != custom_taxon_id.strip():
        raise ValueError("TaxID 必须是正整数。")
    if parsed < 1:
        raise ValueError("TaxID 必须是正整数。")
    return parsed


def summarize_connector_results(
    results: Sequence[ConnectorResult],
) -> ConnectorResultSummary:
    """Count the four mutually exclusive connector outcomes."""

    counts = {
        status: sum(result.status is status for result in results)
        for status in ConnectorStatus
    }
    return ConnectorResultSummary(
        total=len(results),
        online_available=counts[ConnectorStatus.OK],
        no_results=counts[ConnectorStatus.NO_RESULTS],
        offline_export=counts[ConnectorStatus.OFFLINE_EXPORT],
        degraded=counts[ConnectorStatus.DEGRADED],
    )


def restore_connector_entries(
    store: ConnectorArtifactStore,
    run_id: str,
    *,
    limit: int = MAX_RESTORED_CONNECTOR_ENTRIES,
) -> ArchiveRestoreReport:
    """Restore recent archives after path, manifest and SHA-256 verification.

    Entries are returned oldest-to-newest so callers can append them directly
    to bounded session history.  When more than ``limit`` valid archives
    exist, the newest entries are retained.
    """

    if isinstance(limit, bool) or not 1 <= limit <= MAX_RESTORED_CONNECTOR_ENTRIES:
        raise ValueError(
            f"limit 必须在 1 到 {MAX_RESTORED_CONNECTOR_ENTRIES} 之间。"
        )

    run_directory = store.path_for(run_id, "archive-index-probe").parent
    if not run_directory.exists():
        return ArchiveRestoreReport()
    if (
        not run_directory.is_dir()
        or _is_link_like(run_directory)
    ):
        return ArchiveRestoreReport(
            warnings=("运行归档目录不可安全读取，已跳过恢复。",)
        )

    try:
        candidates = sorted(
            run_directory.iterdir(),
            key=lambda item: item.name.casefold(),
        )
    except OSError:
        return ArchiveRestoreReport(
            warnings=("运行归档目录无法读取，已跳过恢复。",)
        )

    entries: list[ConnectorArchiveEntry] = []
    warnings: list[str] = []
    resolved_run_directory = run_directory.resolve()
    for ordinal, candidate in enumerate(candidates, start=1):
        try:
            entry, item_warnings = _restore_one_archive(
                store,
                run_id=run_id,
                candidate=candidate,
                resolved_run_directory=resolved_run_directory,
            )
        except (
            ConnectorArchiveError,
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            warnings.append(
                f"第 {ordinal} 个归档项未通过完整性校验，已跳过。"
            )
            continue
        if entry is None:
            warnings.append(
                f"第 {ordinal} 个归档项不是安全的查询目录，已跳过。"
            )
            continue
        entries.append(entry)
        warnings.extend(
            f"第 {ordinal} 个归档项：{warning}"
            for warning in item_warnings
        )

    entries.sort(key=lambda item: (item.created_at, item.query_id))
    return ArchiveRestoreReport(
        entries=tuple(entries[-limit:]),
        warnings=tuple(warnings),
    )


def _restore_one_archive(
    store: ConnectorArtifactStore,
    *,
    run_id: str,
    candidate: Path,
    resolved_run_directory: Path,
) -> tuple[ConnectorArchiveEntry | None, tuple[str, ...]]:
    if (
        not candidate.is_dir()
        or _is_link_like(candidate)
        or not _SAFE_ARCHIVE_IDENTIFIER.fullmatch(candidate.name)
    ):
        return None, ()
    resolved_candidate = candidate.resolve()
    if resolved_candidate.parent != resolved_run_directory:
        return None, ()

    manifest_path = candidate / "manifest.json"
    result_path = candidate / "result.json"
    _require_safe_regular_file(
        manifest_path,
        parent=resolved_candidate,
        maximum_bytes=_MAX_MANIFEST_BYTES,
    )
    _require_safe_regular_file(
        result_path,
        parent=resolved_candidate,
        maximum_bytes=_MAX_RESULT_BYTES,
    )
    manifest = ConnectorArchiveManifest.model_validate_json(
        manifest_path.read_text(encoding="utf-8")
    )
    if (
        manifest.run_id != run_id
        or manifest.query_id != candidate.name
        or manifest.created_at_utc.utcoffset() is None
    ):
        raise ValueError("archive identity mismatch")

    total_response_bytes = 0
    for expected_index, response in enumerate(manifest.responses, start=1):
        if (
            response.index != expected_index
            or not _SAFE_RESPONSE_FILENAME.fullmatch(response.filename)
        ):
            raise ValueError("unsafe response manifest")
        if response.byte_count > _MAX_RESPONSE_BYTES:
            raise ValueError("archived response exceeds size limit")
        total_response_bytes += response.byte_count
        if total_response_bytes > _MAX_ARCHIVE_RESPONSE_BYTES:
            raise ValueError("archive responses exceed cumulative size limit")
        _require_safe_regular_file(
            candidate / response.filename,
            parent=resolved_candidate,
            maximum_bytes=_MAX_RESPONSE_BYTES,
        )

    verified_manifest = store.load_manifest(run_id, manifest.query_id)
    if verified_manifest != manifest:
        raise ValueError("manifest changed during verification")
    result_bytes = result_path.read_bytes()
    if sha256_bytes(result_bytes) != manifest.result_json_sha256:
        raise ValueError("result changed after verification")
    payload = json.loads(result_bytes.decode("utf-8"))
    result = _restore_result(
        payload,
        manifest=manifest,
        archive_directory=candidate,
    )
    source, source_warning = _infer_database_source(
        result,
        query_id=manifest.query_id,
    )
    item_warnings = (
        ("无法从已验证结果推断数据库来源，已标记为未知来源。",)
        if source_warning
        else ()
    )
    return (
        ConnectorArchiveEntry(
            source=source,
            query_id=manifest.query_id,
            created_at=manifest.created_at_utc,
            archive_path=resolved_candidate.as_posix(),
            result=result,
        ),
        item_warnings,
    )


def _restore_result(
    payload: Any,
    *,
    manifest: ConnectorArchiveManifest,
    archive_directory: Path,
) -> ConnectorResult:
    if not isinstance(payload, dict):
        raise ValueError("result payload must be an object")
    _reject_unredacted_sensitive_fields(payload)
    working = dict(payload)
    metadata = working.pop("export_metadata", None)
    exported_artifacts = working.pop("artifacts", None)
    if not isinstance(exported_artifacts, list):
        raise ValueError("result artifacts must be a list")
    if len(exported_artifacts) != len(manifest.responses):
        raise ValueError("artifact count mismatch")

    artifacts: list[ResponseArtifact] = []
    for exported, response in zip(
        exported_artifacts,
        manifest.responses,
        strict=True,
    ):
        if (
            not isinstance(exported, dict)
            or exported.get("provenance")
            != response.provenance.model_dump(mode="json")
        ):
            raise ValueError("artifact provenance mismatch")
        headers = {
            key: value
            for key, value in {
                "etag": response.response_etag,
                "last-modified": response.response_last_modified,
            }.items()
            if value is not None
        }
        raw_response = (archive_directory / response.filename).read_bytes()
        if (
            len(raw_response) != response.byte_count
            or sha256_bytes(raw_response) != response.sha256
        ):
            raise ValueError("response changed after verification")
        artifacts.append(
            ResponseArtifact(
                provenance=response.provenance,
                raw_response=raw_response,
                response_headers=headers,
            )
        )
    working["artifacts"] = tuple(artifacts)
    result = ConnectorResult.model_validate(working)

    records_sha256 = sha256_bytes(
        canonical_json(result.records).encode("utf-8")
    )
    if (
        result.status.value != manifest.connector_status
        or result.warnings != manifest.warnings
        or records_sha256 != manifest.normalized_records_sha256
    ):
        raise ValueError("result does not match manifest")
    _validate_export_metadata(
        metadata,
        result=result,
        manifest=manifest,
        records_sha256=records_sha256,
    )
    return result


def _validate_export_metadata(
    metadata: Any,
    *,
    result: ConnectorResult,
    manifest: ConnectorArchiveManifest,
    records_sha256: str,
) -> None:
    if not isinstance(metadata, dict):
        raise ValueError("export metadata missing")
    record_hashes = [
        sha256_bytes(canonical_json(record).encode("utf-8"))
        for record in result.records
    ]
    if (
        metadata.get("schema_version") != CONNECTOR_EXPORT_SCHEMA_VERSION
        or metadata.get("parser_version") != manifest.parser_version
        or metadata.get("records_sha256") != records_sha256
        or metadata.get("record_sha256") != record_hashes
        or metadata.get("raw_response_storage")
        != "external_connector_artifacts"
    ):
        raise ValueError("export metadata mismatch")


def _infer_database_source(
    result: ConnectorResult,
    *,
    query_id: str,
) -> tuple[str, bool]:
    direct_names = {
        "pubchem": "PubChem",
        "uniprot": "UniProt",
        "rcsb pdb": "RCSB PDB",
        "string": "STRING",
        "david": "DAVID",
        "omim": "OMIM",
        "drugbank": "DrugBank",
        "genecards": "GeneCards",
        "malacards": "MalaCards",
        "swisstargetprediction": "SwissTargetPrediction",
    }
    for provenance in reversed(result.provenance):
        normalized = provenance.source_name.strip().casefold()
        if normalized in direct_names:
            return direct_names[normalized], False

    record_sources = {
        {
            "gene": "NCBI Gene",
            "nucleotide": "GenBank",
            "compound": "PubChem",
            "protein": "UniProt",
            "structure": "RCSB PDB",
            "structure_hit": "RCSB PDB",
            "string_interaction": "STRING",
            "david_enrichment": "DAVID",
            "omim_entry": "OMIM",
            "drugbank_drug": "DrugBank",
            "drugbank_bond": "DrugBank",
            "genecards_gene": "GeneCards",
            "malacards_disease": "MalaCards",
            "swiss_target_prediction": "SwissTargetPrediction",
        }[record_type]
        for record in result.records
        if (
            isinstance(record, Mapping)
            and isinstance((record_type := record.get("record_type")), str)
            and record_type
            in {
                "gene",
                "nucleotide",
                "compound",
                "protein",
                "structure",
                "structure_hit",
                "string_interaction",
                "david_enrichment",
                "omim_entry",
                "drugbank_drug",
                "drugbank_bond",
                "genecards_gene",
                "malacards_disease",
                "swiss_target_prediction",
            }
        )
    }
    if len(record_sources) == 1:
        return record_sources.pop(), False

    prefixes = (
        ("ncbi-gene-", "NCBI Gene"),
        ("rcsb-pdb-", "RCSB PDB"),
        ("pubchem-", "PubChem"),
        ("uniprot-", "UniProt"),
        ("genbank-", "GenBank"),
        ("string-", "STRING"),
        ("david-", "DAVID"),
        ("omim-", "OMIM"),
        ("drugbank-", "DrugBank"),
        ("genecards-", "GeneCards"),
        ("malacards-", "MalaCards"),
        ("swiss-target-prediction-", "SwissTargetPrediction"),
    )
    normalized_query_id = query_id.casefold()
    for prefix, label in prefixes:
        if normalized_query_id.startswith(prefix):
            return label, False
    return "未知来源", True


def _require_safe_regular_file(
    path: Path,
    *,
    parent: Path,
    maximum_bytes: int | None = None,
) -> None:
    if not path.is_file() or _is_link_like(path):
        raise ValueError("archive member is not a regular file")
    resolved = path.resolve()
    if resolved.parent != parent:
        raise ValueError("archive member escapes its directory")
    if maximum_bytes is not None and path.stat().st_size > maximum_bytes:
        raise ValueError("archive member exceeds size limit")


def _is_link_like(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(is_junction and is_junction())


def _reject_unredacted_sensitive_fields(
    value: Any,
    *,
    parent_key: str | None = None,
) -> None:
    if parent_key and _is_sensitive_field(parent_key):
        if value is None:
            return
        if (
            isinstance(value, str)
            and value.strip().casefold() in _SAFE_REDACTED_VALUES
        ):
            return
        raise ValueError("unredacted sensitive field")

    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_unredacted_sensitive_fields(
                item,
                parent_key=str(key),
            )
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _reject_unredacted_sensitive_fields(item)
        return
    if isinstance(value, str):
        stripped = value.strip()
        if (
            len(stripped) <= _MAX_MANIFEST_BYTES
            and stripped[:1] in {"{", "["}
            and stripped[-1:] in {"}", "]"}
        ):
            try:
                nested = json.loads(stripped)
            except json.JSONDecodeError:
                return
            _reject_unredacted_sensitive_fields(nested)


def _is_sensitive_field(value: str) -> bool:
    normalized = value.strip().casefold().replace("-", "_")
    return (
        normalized in _SENSITIVE_FIELD_NAMES
        or normalized.endswith("_api_key")
        or normalized.endswith("_password")
        or normalized.endswith("_token")
    )


__all__ = [
    "ArchiveRestoreReport",
    "ConnectorArchiveEntry",
    "ConnectorResultSummary",
    "CUSTOM_TAXON_LABEL",
    "DAVID_SUPPORTED_TAXON_IDS",
    "DATABASE_SOURCE_CONFIG_BY_KEY",
    "DATABASE_SOURCE_CONFIGS",
    "DatabaseSourceAccessMode",
    "DatabaseSourceKey",
    "MAX_RESTORED_CONNECTOR_ENTRIES",
    "SWISS_TARGET_SUPPORTED_TAXON_IDS",
    "VETERINARY_SPECIES_TAX_IDS",
    "DatabaseSourceConfig",
    "parse_taxon_selection",
    "restore_connector_entries",
    "summarize_connector_results",
]
