"""Strict adapters used by the Streamlit docking workbench.

The helpers keep network retrieval and batch-metadata parsing outside the UI
rerun loop.  They do not prepare structures or execute scientific software.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from vetevidence.docking_workflow import LigandBatchItem, LigandIdentity


_MAX_RCSB_STRUCTURE_BYTES = 50 * 1024 * 1024
_MAX_LIGAND_METADATA_BYTES = 1024 * 1024
_MAX_LIGANDS = 100
_MAX_LIGAND_FILE_BYTES = 25 * 1024 * 1024
_MAX_LIGAND_BATCH_BYTES = 100 * 1024 * 1024
_MAX_DOCKING_ATTEMPTS = 24
_MAX_DOCKING_WORK_UNITS = 384
_PDB_ID_PATTERN = re.compile(r"^[0-9][A-Za-z0-9]{3}$")
_SAFE_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_METADATA_COLUMNS = (
    "filename",
    "ligand_id",
    "compound_name",
    "namespace",
    "pubchem_cid",
    "inchikey",
    "user_namespace",
    "user_accession",
    "source_url",
    "source_revision",
)


class _HTTPClient(Protocol):
    def get(self, url: str, **kwargs: object) -> httpx.Response: ...


class DockingUISupportModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )


class RCSBStructureDownload(DockingUISupportModel):
    pdb_id: str = Field(pattern=r"^[0-9][A-Za-z0-9]{3}$")
    filename: str
    source_url: str
    source_revision: str
    accessed_at: datetime
    payload: bytes = Field(min_length=1)
    sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class DockingWorkloadEstimate(DockingUISupportModel):
    ligand_count: int = Field(ge=1)
    seed_count: int = Field(ge=1)
    attempt_count: int = Field(ge=1)
    exhaustiveness: int = Field(ge=1)
    work_units: int = Field(ge=1)
    ligand_upload_bytes: int = Field(ge=1)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def ligand_metadata_template() -> bytes:
    """Return a UTF-8 CSV showing both supported identity namespaces."""

    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=_METADATA_COLUMNS)
    writer.writeheader()
    writer.writerow(
        {
            "filename": "quercetin.pdbqt",
            "ligand_id": "quercetin",
            "compound_name": "quercetin",
            "namespace": "pubchem",
            "pubchem_cid": "5280343",
            "inchikey": "REFJWTPEDVJJIY-UHFFFAOYSA-N",
            "source_url": (
                "https://pubchem.ncbi.nlm.nih.gov/compound/5280343"
            ),
            "source_revision": "record-modified:YYYY-MM-DD",
        }
    )
    writer.writerow(
        {
            "filename": "candidate-001.sdf",
            "ligand_id": "candidate-001",
            "compound_name": "candidate-001",
            "namespace": "user",
            "user_namespace": "lab",
            "user_accession": "candidate-001",
            "source_revision": "experiment-batch-or-file-version",
        }
    )
    return buffer.getvalue().encode("utf-8-sig")


def parse_vina_seeds(value: str, *, maximum_count: int = 12) -> tuple[int, ...]:
    """Parse a bounded comma/space/semicolon separated list of explicit seeds."""

    if maximum_count < 1:
        raise ValueError("maximum_count 必须大于 0。")
    tokens = [
        token
        for token in re.split(r"[\s,;，；]+", value.strip())
        if token
    ]
    if not tokens:
        raise ValueError("至少填写一个 Vina seed。")
    if len(tokens) > maximum_count:
        raise ValueError(f"单批最多允许 {maximum_count} 个 Vina seed。")
    try:
        seeds = tuple(int(token) for token in tokens)
    except ValueError as exc:
        raise ValueError("Vina seed 只能是整数。") from exc
    if len(seeds) != len(set(seeds)):
        raise ValueError("Vina seed 不能重复。")
    if any(seed < -(2**31) or seed > 2**31 - 1 for seed in seeds):
        raise ValueError("Vina seed 必须是 32 位有符号整数。")
    return seeds


def _decode_csv(payload: bytes) -> str:
    if not payload:
        raise ValueError("配体身份 CSV 为空。")
    if len(payload) > _MAX_LIGAND_METADATA_BYTES:
        raise ValueError("配体身份 CSV 超过 1 MB 上限。")
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("配体身份 CSV 必须使用 UTF-8 编码。") from exc


def build_ligand_batch_items(
    files: Mapping[str, bytes],
    metadata_csv: bytes,
) -> tuple[LigandBatchItem, ...]:
    """Bind each uploaded ligand to one strict, hash-checked identity row."""

    if not files:
        raise ValueError("至少上传一个配体文件。")
    if len(files) > _MAX_LIGANDS:
        raise ValueError(f"单批最多允许 {_MAX_LIGANDS} 个配体文件。")
    normalized_files: dict[str, bytes] = {}
    for filename, payload in files.items():
        safe_name = PurePath(filename).name
        if safe_name != filename or not _SAFE_FILENAME_PATTERN.fullmatch(
            safe_name
        ):
            raise ValueError(f"配体文件名不安全：{filename!r}。")
        if safe_name in normalized_files:
            raise ValueError(f"配体文件名重复：{safe_name}。")
        if not payload:
            raise ValueError(f"配体文件为空：{safe_name}。")
        if len(payload) > _MAX_LIGAND_FILE_BYTES:
            raise ValueError(f"配体文件超过 25 MB 上限：{safe_name}。")
        normalized_files[safe_name] = payload
    if sum(map(len, normalized_files.values())) > _MAX_LIGAND_BATCH_BYTES:
        raise ValueError("配体文件总量超过 100 MB 上限。")

    reader = csv.DictReader(io.StringIO(_decode_csv(metadata_csv), newline=""))
    if tuple(reader.fieldnames or ()) != _METADATA_COLUMNS:
        raise ValueError(
            "配体身份 CSV 表头必须与模板完全一致，且顺序不能改变。"
        )
    rows = list(reader)
    if not rows:
        raise ValueError("配体身份 CSV 没有数据行。")
    if len(rows) > _MAX_LIGANDS:
        raise ValueError(f"配体身份 CSV 最多 {_MAX_LIGANDS} 行。")

    seen: set[str] = set()
    items: list[LigandBatchItem] = []
    for row_number, row in enumerate(rows, start=2):
        filename = (row.get("filename") or "").strip()
        if filename in seen:
            raise ValueError(f"CSV 第 {row_number} 行文件名重复：{filename}。")
        seen.add(filename)
        payload = normalized_files.get(filename)
        if payload is None:
            raise ValueError(
                f"CSV 第 {row_number} 行引用了未上传文件：{filename}。"
            )
        namespace = (row.get("namespace") or "").strip().casefold()
        common = {
            "namespace": namespace,
            "structure_sha256": _sha256(payload),
            "source_revision": (row.get("source_revision") or "").strip(),
        }
        if namespace == "pubchem":
            cid_text = (row.get("pubchem_cid") or "").strip()
            try:
                cid = int(cid_text)
            except ValueError as exc:
                raise ValueError(
                    f"CSV 第 {row_number} 行 PubChem CID 必须是整数。"
                ) from exc
            identity = LigandIdentity(
                **common,
                pubchem_cid=cid,
                inchikey=(row.get("inchikey") or "").strip().upper(),
                source_url=(row.get("source_url") or "").strip(),
            )
        elif namespace == "user":
            identity = LigandIdentity(
                **common,
                user_namespace=(row.get("user_namespace") or "").strip(),
                user_accession=(row.get("user_accession") or "").strip(),
                source_url=(row.get("source_url") or "").strip() or None,
            )
        else:
            raise ValueError(
                f"CSV 第 {row_number} 行 namespace 只能是 pubchem 或 user。"
            )
        suffix = PurePath(filename).suffix.removeprefix(".").casefold()
        items.append(
            LigandBatchItem(
                ligand_id=(row.get("ligand_id") or "").strip(),
                compound_name=(row.get("compound_name") or "").strip(),
                identity=identity,
                filename=filename,
                input_format=suffix,
                original_payload=payload,
            )
        )
    extra_files = sorted(set(normalized_files) - seen)
    if extra_files:
        raise ValueError(
            "以下上传文件没有对应身份行：" + "、".join(extra_files)
        )
    ligand_ids = [item.ligand_id for item in items]
    if len(ligand_ids) != len(set(ligand_ids)):
        raise ValueError("配体身份 CSV 的 ligand_id 不能重复。")
    return tuple(items)


def validate_docking_workload(
    ligands: Sequence[LigandBatchItem],
    seeds: Sequence[int],
    *,
    exhaustiveness: int,
    maximum_attempts: int = _MAX_DOCKING_ATTEMPTS,
    maximum_work_units: int = _MAX_DOCKING_WORK_UNITS,
    maximum_upload_bytes: int = _MAX_LIGAND_BATCH_BYTES,
) -> DockingWorkloadEstimate:
    """Bound synchronous Vina work before the Streamlit thread starts it."""

    if not ligands or not seeds:
        raise ValueError("配体和 Vina seed 均不能为空。")
    if exhaustiveness < 1:
        raise ValueError("exhaustiveness 必须大于 0。")
    if min(maximum_attempts, maximum_work_units, maximum_upload_bytes) < 1:
        raise ValueError("工作负载上限必须大于 0。")
    attempt_count = len(ligands) * len(seeds)
    work_units = attempt_count * exhaustiveness
    upload_bytes = sum(len(item.original_payload) for item in ligands)
    if upload_bytes > maximum_upload_bytes:
        raise ValueError("配体文件总量超过当前同步任务上限。")
    if attempt_count > maximum_attempts:
        raise ValueError(
            f"当前同步任务最多 {maximum_attempts} 次 Vina 尝试；"
            "请拆分批次。"
        )
    if work_units > maximum_work_units:
        raise ValueError(
            f"尝试数 × exhaustiveness 最多 {maximum_work_units}；"
            "请减少种子、配体或 exhaustiveness。"
        )
    return DockingWorkloadEstimate(
        ligand_count=len(ligands),
        seed_count=len(seeds),
        attempt_count=attempt_count,
        exhaustiveness=exhaustiveness,
        work_units=work_units,
        ligand_upload_bytes=upload_bytes,
    )


def fetch_rcsb_pdb(
    pdb_id: str,
    *,
    client: _HTTPClient | None = None,
    accessed_at: datetime | None = None,
) -> RCSBStructureDownload:
    """Fetch one public PDB coordinate file after an explicit UI submit."""

    normalized = pdb_id.strip().upper()
    if not _PDB_ID_PATTERN.fullmatch(normalized):
        raise ValueError("PDB ID 必须是以数字开头的 4 位字母数字标识。")
    url = f"https://files.rcsb.org/download/{normalized}.pdb"
    owns_client = client is None
    active_client = client or httpx.Client(
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
        headers={"User-Agent": "VetEvidenceAI/0.5"},
    )
    try:
        response = active_client.get(
            url,
            headers={"Accept": "chemical/x-pdb,text/plain"},
        )
        response.raise_for_status()
        payload = response.content
    finally:
        if owns_client:
            active_client.close()  # type: ignore[attr-defined]
    if not payload:
        raise ValueError("RCSB 返回了空结构文件。")
    if len(payload) > _MAX_RCSB_STRUCTURE_BYTES:
        raise ValueError("RCSB 结构文件超过 50 MB 上限。")
    if b"\x00" in payload[:4096]:
        raise ValueError("RCSB 结构文件包含二进制 NUL，不是文本 PDB。")
    revision = (
        response.headers.get("last-modified")
        or response.headers.get("etag", "").strip('"')
        or "not-reported"
    )
    return RCSBStructureDownload(
        pdb_id=normalized,
        filename=f"{normalized}.pdb",
        source_url=url,
        source_revision=revision,
        accessed_at=accessed_at or datetime.now(timezone.utc),
        payload=payload,
        sha256=_sha256(payload),
    )


__all__ = [
    "RCSBStructureDownload",
    "build_ligand_batch_items",
    "fetch_rcsb_pdb",
    "ligand_metadata_template",
    "parse_vina_seeds",
]
