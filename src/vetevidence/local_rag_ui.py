"""Streamlit surface for the no-key, local-only literature retrieval path."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any

import streamlit as st
from pydantic import ValidationError

from vetevidence.literature_import import LiteratureImportResult
from vetevidence.local_rag import IndexManifest
from vetevidence.models import ResearchResult
from vetevidence.workbench import LiteratureEvidenceGrade
from vetevidence.workbench_pipeline import ExperimentCondition
from vetevidence.workbench_rag import (
    MAX_WORKBENCH_RAG_QUERY_CHARACTERS,
    SearchMode,
    WORKBENCH_RAG_VERSION,
    WorkbenchRAGSearchOutcome,
    build_workbench_rag_index,
    literature_import_sha256,
    open_workbench_rag_index,
    prepare_workbench_rag_sources,
    search_workbench_rag,
    source_set_sha256,
    workbench_rag_index_is_current,
)


AuditCallback = Callable[..., None]
_SEARCH_MODE_LABELS: dict[SearchMode, str] = {
    "keyword_only": "关键词（推荐）",
    "hash_vector_only": "特征哈希（实验）",
    "hybrid": "混合（实验）",
}
_GRADE_LABELS = {
    LiteratureEvidenceGrade.DIRECT_INTERACTION: "直接文献证据",
    LiteratureEvidenceGrade.CONTEXTUAL: "间接背景",
    LiteratureEvidenceGrade.OUT_OF_SCOPE: "主题不匹配",
    LiteratureEvidenceGrade.UNASSESSED: "未评估",
}
_LOCAL_RAG_ERRORS = (OSError, sqlite3.Error, TypeError, ValueError)


def _record(callback: AuditCallback | None, **payload: Any) -> None:
    if callback is not None:
        callback(**payload)


def _zero_cost_metadata() -> dict[str, object]:
    return {
        "workbench_rag_version": WORKBENCH_RAG_VERSION,
        "embedding_fake": True,
        "embedding_network_used": False,
        "network_used": False,
        "network_calls": 0,
        "real_model_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "model_api_cost_cny": 0,
        "external_actions": 0,
    }


def _manifest_metadata(manifest: IndexManifest) -> dict[str, object]:
    return {
        **_zero_cost_metadata(),
        "source_count": manifest.source_count,
        "chunk_count": manifest.chunk_count,
        "source_manifest_sha256": manifest.source_manifest_sha256,
        "chunk_manifest_sha256": manifest.chunk_manifest_sha256,
        "embedding_bytes_sha256": manifest.embedding_bytes_sha256,
        "embedding_provider_name": manifest.embedding_provider_name,
        "embedding_model_name": manifest.embedding_model_name,
        "embedding_model_version": manifest.embedding_model_version,
        "embedding_dimensions": manifest.embedding_dimensions,
        "embedding_fake": manifest.embedding_fake,
        "embedding_network_used": manifest.embedding_network_used,
    }


def _safe_local_error(exc: BaseException, index_file: Path) -> str:
    """Keep local filesystem locations out of UI errors and audit records."""

    message = " ".join(str(exc).split()) or type(exc).__name__
    candidates = {str(index_file), str(index_file.parent)}
    try:
        candidates.update(
            {
                str(index_file.resolve()),
                str(index_file.parent.resolve()),
            }
        )
    except OSError:
        pass
    path_variants = {
        variant
        for candidate in candidates
        for variant in (
            candidate,
            candidate.replace("\\", "\\\\"),
            candidate.replace("\\", "/"),
        )
        if variant
    }
    for candidate in sorted(path_variants, key=len, reverse=True):
        message = message.replace(candidate, "<local-index>")
    return message


def _load_outcome(state_key: str) -> WorkbenchRAGSearchOutcome | None:
    payload = st.session_state.get(state_key)
    if payload is None:
        return None
    try:
        return WorkbenchRAGSearchOutcome.model_validate(payload)
    except (ValidationError, ValueError, TypeError):
        st.session_state.pop(state_key, None)
        return None


def _result_rows(
    outcome: WorkbenchRAGSearchOutcome,
    conditions: Sequence[ExperimentCondition],
) -> list[dict[str, object]]:
    grade_by_source = {
        condition.source_id: _GRADE_LABELS[condition.qualification.grade]
        for condition in conditions
    }
    rows: list[dict[str, object]] = []
    for result in outcome.results:
        chunk = result.chunk
        rows.append(
            {
                "名次": result.rank,
                "来源 ID": chunk.source_id,
                "证据准入": grade_by_source.get(chunk.source_id, "未评估"),
                "题名": chunk.title,
                "PMID": chunk.pmid or "",
                "DOI": chunk.doi or "",
                "来源 URL（纯文本）": chunk.source_url or "",
                "字段位置": chunk.field_location,
                "授权范围": chunk.authorization_scope,
                "数据状态": chunk.metadata.get("data_status", ""),
                "综合分数": round(result.score, 6),
                "关键词分数": round(result.keyword_score, 6),
                "向量分数": round(result.vector_score, 6),
                "原文片段（不可信数据）": chunk.content,
                "来源全文 SHA-256": chunk.source_content_sha256,
                "片段 SHA-256": chunk.content_sha256,
            }
        )
    return rows


def render_local_rag_workbench(
    *,
    run_id: str,
    research: ResearchResult | None,
    imported: LiteratureImportResult | None,
    conditions: Sequence[ExperimentCondition],
    index_path: str | Path,
    audit_callback: AuditCallback | None = None,
) -> None:
    """Render local index/build/search controls for one workbench run."""

    st.header("本地证据检索（免费模式）")
    st.caption(
        "只在本机检索当前任务的 PubMed 摘要和你确认有权使用的导入摘要。"
        "不读取模型 Key、不联网、不生成科研结论，模型 API 费用为 0。"
    )
    st.warning(
        "检索命中只是待人工审查的候选片段，不等于证据支持。最终结论继续使用"
        "现有证据准入规则和人工复核；所有正文均按不可信数据处理。"
    )

    imported_count = len(imported.records) if imported is not None else 0
    include_imports = False
    import_fingerprint: str | None = None
    if imported_count:
        authorization_key = f"local-rag-authorize-import-{run_id}"
        authorization_fingerprint_key = (
            f"local-rag-authorize-import-fingerprint-{run_id}"
        )
        import_fingerprint = literature_import_sha256(imported)
        if (
            st.session_state.get(authorization_fingerprint_key)
            != import_fingerprint
        ):
            st.session_state[authorization_key] = False
            st.session_state[authorization_fingerprint_key] = import_fingerprint
        include_imports = st.checkbox(
            "我确认当前导入题录有权用于本机索引",
            key=authorization_key,
            help=(
                "只授权本机建立索引，不代表系统已向原平台认证许可，也不授权"
                "重新分发原始材料。"
            ),
            persist_state="session",
        )

    try:
        prepared = prepare_workbench_rag_sources(
            run_id=run_id,
            research=research,
            imported=imported,
            include_user_authorized_imports=include_imports,
        )
    except (TypeError, ValueError) as exc:
        st.error(f"当前材料不能建立本地索引：{exc}")
        return

    authorization_metadata: dict[str, object] = {
        "public_source_count": prepared.public_source_count,
        "user_authorized_source_count": prepared.user_authorized_source_count,
        "user_authorized_imports_confirmed": bool(
            include_imports and imported_count
        ),
        "authorized_import_sha256": (
            import_fingerprint if include_imports else None
        ),
    }

    metric_columns = st.columns(4)
    metric_columns[0].metric("可索引摘要", len(prepared.sources))
    metric_columns[1].metric("PubMed 公开摘要", prepared.public_source_count)
    metric_columns[2].metric(
        "已确认导入摘要",
        prepared.user_authorized_source_count,
    )
    metric_columns[3].metric(
        "本地字符数",
        f"{prepared.total_character_count:,}",
    )
    if prepared.excluded_unconfirmed_import_count:
        st.info(
            f"另有 {prepared.excluded_unconfirmed_import_count} 条导入题录尚未获得"
            "本机索引确认，当前不会进入索引。"
        )
    if prepared.skipped_missing_abstract_count:
        st.info(
            f"已排除 {prepared.skipped_missing_abstract_count} 条缺少摘要的记录；"
            "仅有题名不能支持实验细节检索。"
        )
    if prepared.synthetic_source_count:
        st.warning(
            f"当前来源中有 {prepared.synthetic_source_count} 条合成演示材料；"
            "它们只用于流程测试，不得作为科研证据。"
        )
    st.caption(
        "索引以普通 SQLite 文件保存在本机 .workbench 目录，当前未做静态加密。"
    )

    index_file = Path(index_path)
    state_key = f"local-rag-outcome-{run_id}"
    manifest: IndexManifest | None = None
    index_current = False
    index_error: str | None = None
    if index_file.exists():
        try:
            manifest = open_workbench_rag_index(index_file).manifest()
            index_current = workbench_rag_index_is_current(
                index_file,
                prepared.sources,
            )
        except _LOCAL_RAG_ERRORS as exc:
            index_error = _safe_local_error(exc, index_file)
            st.session_state.pop(state_key, None)

    build_disabled = not prepared.sources
    if st.button(
        "建立或更新本地索引",
        type="primary",
        icon=":material/database:",
        width="stretch",
        key=f"local-rag-build-{run_id}",
        disabled=build_disabled,
    ):
        try:
            with st.spinner("正在本机切片、计算特征哈希并写入 SQLite…"):
                manifest = build_workbench_rag_index(
                    index_file,
                    prepared.sources,
                )
            index_current = True
            index_error = None
            st.session_state.pop(state_key, None)
        except _LOCAL_RAG_ERRORS as exc:
            index_current = False
            index_error = _safe_local_error(exc, index_file)
            _record(
                audit_callback,
                tool_name="local_rag.build",
                input_summary=(
                    f"source_set_sha256={source_set_sha256(prepared.sources)}"
                ),
                status="failed",
                error=index_error,
                metadata={
                    **_zero_cost_metadata(),
                    **authorization_metadata,
                },
            )
        else:
            metadata = _manifest_metadata(manifest)
            metadata.update(authorization_metadata)
            _record(
                audit_callback,
                tool_name="local_rag.build",
                input_summary=(
                    f"source_set_sha256={source_set_sha256(prepared.sources)}"
                ),
                status="succeeded",
                output_summary=(
                    f"本地索引包含 {manifest.source_count} 个来源、"
                    f"{manifest.chunk_count} 个切片"
                ),
                metadata=metadata,
            )
            st.success("本地索引已建立；没有访问网络或模型 API。")

    if build_disabled:
        st.info("请先完成 PubMed 检索，或导入并确认有权使用的摘要。")
    elif index_error:
        st.error(f"本地索引损坏或不兼容，请重新建立：{index_error}")
    elif manifest is None:
        st.info("尚未建立当前任务的本地索引。")
    elif not index_current:
        st.warning("当前文献集合已变化，旧索引已过期；请重新建立后再检索。")
        st.session_state.pop(state_key, None)
    else:
        st.success(
            f"索引可用：{manifest.source_count} 个来源，"
            f"{manifest.chunk_count} 个切片。"
        )

    if not index_current:
        st.session_state.pop(state_key, None)

    if manifest is not None:
        with st.expander("查看本地索引清单与哈希", icon=":material/fingerprint:"):
            st.json(_manifest_metadata(manifest), expanded=True)

    with st.form(f"local-rag-search-form-{run_id}"):
        query = st.text_input(
            "在当前证据中检索",
            placeholder="例如：FICI synergy quercetin",
            max_chars=MAX_WORKBENCH_RAG_QUERY_CHARACTERS,
            key=f"local-rag-query-{run_id}",
            persist_state="session",
        )
        mode = st.segmented_control(
            "检索模式",
            options=tuple(_SEARCH_MODE_LABELS),
            default="keyword_only",
            required=True,
            format_func=lambda item: _SEARCH_MODE_LABELS[item],
            key=f"local-rag-mode-{run_id}",
            width="stretch",
            persist_state="session",
            help=(
                "固定评测中关键词优于当前特征哈希，因此关键词是默认模式；"
                "另外两种只用于工程实验，不是真实语义模型。"
            ),
        )
        submitted = st.form_submit_button(
            "检索当前证据",
            icon=":material/search:",
            width="stretch",
            disabled=not (manifest is not None and index_current),
        )

    outcome = _load_outcome(state_key)
    if submitted:
        clean_query = " ".join(query.split())
        query_sha256 = sha256(clean_query.encode("utf-8")).hexdigest()
        try:
            outcome = search_workbench_rag(
                index_file,
                clean_query,
                mode=mode,
                limit=3,
            )
        except _LOCAL_RAG_ERRORS as exc:
            st.session_state.pop(state_key, None)
            outcome = None
            safe_error = _safe_local_error(exc, index_file)
            _record(
                audit_callback,
                tool_name="local_rag.search",
                input_summary=f"query_sha256={query_sha256}",
                status="failed",
                error=safe_error,
                metadata={
                    **(
                        _manifest_metadata(manifest)
                        if manifest is not None
                        else _zero_cost_metadata()
                    ),
                    **authorization_metadata,
                    "mode": mode,
                },
            )
            st.error(f"本地检索失败：{safe_error}")
        else:
            st.session_state[state_key] = outcome.model_dump(mode="json")
            _record(
                audit_callback,
                tool_name="local_rag.search",
                input_summary=f"query_sha256={outcome.query_sha256}",
                status="succeeded",
                output_summary=(
                    f"status={outcome.retrieval_status}; "
                    f"candidate_count={len(outcome.results)}"
                ),
                metadata={
                    **_manifest_metadata(manifest),
                    **authorization_metadata,
                    "mode": outcome.mode,
                    "retrieval_status": outcome.retrieval_status,
                    "result_source_ids": [
                        result.chunk.source_id for result in outcome.results
                    ],
                    "result_chunk_ids": [
                        result.chunk.chunk_id for result in outcome.results
                    ],
                },
            )

    if outcome is None:
        return

    outcome_columns = st.columns(4)
    outcome_columns[0].metric("候选片段", len(outcome.results))
    outcome_columns[1].metric("网络调用", outcome.network_calls)
    outcome_columns[2].metric("真实模型调用", outcome.real_model_calls)
    outcome_columns[3].metric("模型 API 费用", "¥0")
    if outcome.retrieval_status == "insufficient_evidence":
        st.warning(
            "insufficient_evidence：当前模式没有找到正分候选，不返回零分结果凑数。"
        )
        return

    st.info(
        "已找到待审查候选；它们仍需经过证据准入和人工复核，不能直接写成结论。"
    )
    rows = _result_rows(outcome, conditions)
    st.dataframe(
        rows,
        hide_index=True,
        key=f"local-rag-results-{run_id}",
    )
    st.download_button(
        "下载本次候选与溯源 JSON",
        data=json.dumps(
            outcome.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        ),
        file_name=f"{run_id}-local-rag-results.json",
        mime="application/json",
        icon=":material/download:",
        key=f"local-rag-download-{run_id}",
    )


__all__ = ["render_local_rag_workbench"]
