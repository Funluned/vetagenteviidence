"""Deterministic, local-only hybrid retrieval for traceable evidence chunks.

Evidence text is data, never an instruction channel.  The index therefore marks
every source and chunk as ``untrusted_evidence`` and only returns typed records;
it does not interpret text or execute tools.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import tempfile
import unicodedata
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from vetevidence.agent_providers import (
    DeterministicHashEmbeddingProvider,
    EmbeddingProvider,
)


INDEX_SCHEMA_VERSION = 1
CHUNKER_VERSION = "fixed-char-v1"
EVIDENCE_ROLE = "untrusted_evidence"
AuthorizationScope = Literal["public", "licensed", "user_authorized"]
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+|[\u3400-\u4dbf\u4e00-\u9fff]+")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_text(value: str) -> str:
    """Normalize Unicode/newlines while preserving evidence wording."""

    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace(
        "\r", "\n"
    ).strip()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class _RAGModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
    )


class EvidenceSource(_RAGModel):
    """One authorized evidence field before deterministic chunking."""

    source_id: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1, repr=False)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    field_location: str = Field(min_length=1)
    version: str = Field(min_length=1)
    authorization_scope: AuthorizationScope
    pmid: str | None = None
    doi: str | None = None
    source_url: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    evidence_role: Literal["untrusted_evidence"] = EVIDENCE_ROLE

    @model_validator(mode="before")
    @classmethod
    def canonicalize_and_hash(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        content = payload.get("content")
        if not isinstance(content, str):
            return payload
        canonical = _canonical_text(content)
        if not canonical:
            raise ValueError("证据正文不能为空。")
        expected = _sha256_text(canonical)
        supplied = payload.get("content_sha256")
        if supplied is not None and str(supplied).strip().lower() != expected:
            raise ValueError("来源正文与 content_sha256 不一致。")
        payload["content"] = canonical
        payload["content_sha256"] = expected
        return payload

    @field_validator("pmid", "doi", "source_url", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip() or None
        return value


class EvidenceChunk(_RAGModel):
    """A stable text slice with complete source provenance."""

    chunk_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_type: str = Field(min_length=1)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1, repr=False)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_content_sha256: str = Field(pattern=_SHA256_PATTERN)
    field_location: str = Field(min_length=1)
    source_field_location: str = Field(min_length=1)
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    version: str = Field(min_length=1)
    authorization_scope: AuthorizationScope
    pmid: str | None = None
    doi: str | None = None
    source_url: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    evidence_role: Literal["untrusted_evidence"] = EVIDENCE_ROLE

    @model_validator(mode="after")
    def validate_slice(self) -> EvidenceChunk:
        if self.end_char <= self.start_char:
            raise ValueError("切片结束位置必须大于开始位置。")
        if _sha256_text(self.content) != self.content_sha256:
            raise ValueError("切片正文与 content_sha256 不一致。")
        return self


class RAGMetadataFilter(_RAGModel):
    """Exact-match metadata filters; populated fields are combined with AND."""

    source_ids: tuple[str, ...] = ()
    source_types: tuple[str, ...] = ()
    pmids: tuple[str, ...] = ()
    dois: tuple[str, ...] = ()
    versions: tuple[str, ...] = ()
    authorization_scopes: tuple[AuthorizationScope, ...] = ()
    metadata_equals: dict[str, str] = Field(default_factory=dict)


class RetrievalResult(_RAGModel):
    """One hybrid retrieval hit; its text remains untrusted evidence data."""

    rank: int = Field(ge=1)
    chunk: EvidenceChunk
    score: float
    keyword_score: float
    vector_score: float
    evidence_role: Literal["untrusted_evidence"] = EVIDENCE_ROLE


class IndexManifest(_RAGModel):
    schema_version: Literal[1] = INDEX_SCHEMA_VERSION
    chunker_version: Literal["fixed-char-v1"] = CHUNKER_VERSION
    chunk_size: int = Field(gt=0)
    overlap_chars: int = Field(ge=0)
    source_count: int = Field(ge=1)
    chunk_count: int = Field(ge=1)
    source_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    chunk_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    embedding_bytes_sha256: str = Field(pattern=_SHA256_PATTERN)
    embedding_provider_name: str = Field(min_length=1)
    embedding_model_name: str = Field(min_length=1)
    embedding_model_version: str = Field(min_length=1)
    embedding_dimensions: int = Field(gt=0)
    embedding_fake: bool
    embedding_network_used: Literal[False] = False


def chunk_source(
    source: EvidenceSource,
    *,
    chunk_size: int = 1_200,
    overlap_chars: int = 120,
) -> list[EvidenceChunk]:
    """Split one canonical source into stable, overlapping character windows."""

    if chunk_size < 1:
        raise ValueError("chunk_size 必须大于 0。")
    if overlap_chars < 0 or overlap_chars >= chunk_size:
        raise ValueError("overlap_chars 必须在 0（含）与 chunk_size（不含）之间。")

    chunks: list[EvidenceChunk] = []
    start = 0
    while start < len(source.content):
        end = min(start + chunk_size, len(source.content))
        content = source.content[start:end]
        content_sha256 = _sha256_text(content)
        field_location = f"{source.field_location}#chars={start}:{end}"
        identity = {
            "chunker_version": CHUNKER_VERSION,
            "source_id": source.source_id,
            "source_content_sha256": source.content_sha256,
            "field_location": field_location,
            "content_sha256": content_sha256,
        }
        chunk_id = f"chunk-{_sha256_text(_canonical_json(identity))}"
        chunks.append(
            EvidenceChunk(
                chunk_id=chunk_id,
                source_id=source.source_id,
                source_type=source.source_type,
                title=source.title,
                content=content,
                content_sha256=content_sha256,
                source_content_sha256=source.content_sha256,
                field_location=field_location,
                source_field_location=source.field_location,
                start_char=start,
                end_char=end,
                version=source.version,
                authorization_scope=source.authorization_scope,
                pmid=source.pmid,
                doi=source.doi,
                source_url=source.source_url,
                metadata=source.metadata,
            )
        )
        if end == len(source.content):
            break
        start = end - overlap_chars
    return chunks


def _tokenize(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    tokens: list[str] = []
    for segment in _TOKEN_PATTERN.findall(normalized):
        if re.fullmatch(r"[a-z0-9]+", segment):
            tokens.append(segment)
            continue
        characters = list(segment)
        tokens.extend(characters)
        tokens.extend(
            "".join(characters[index : index + 2])
            for index in range(len(characters) - 1)
        )
    return tokens


def _bm25_scores(query: str, chunks: Sequence[EvidenceChunk]) -> list[float]:
    query_terms = tuple(dict.fromkeys(_tokenize(query)))
    # ``content`` is the canonical text selected by the caller for indexing.
    # ``title`` is retained separately for provenance and display, so adding it
    # here would silently count titles twice when content already includes one.
    documents = [_tokenize(chunk.content) for chunk in chunks]
    if not query_terms or not documents:
        return [0.0 for _ in documents]
    average_length = sum(len(document) for document in documents) / len(documents)
    if average_length == 0:
        return [0.0 for _ in documents]
    document_frequencies = {
        term: sum(term in document for document in documents)
        for term in query_terms
    }
    k1 = 1.5
    b = 0.75
    scores: list[float] = []
    for document in documents:
        frequencies = Counter(document)
        score = 0.0
        for term in query_terms:
            frequency = frequencies[term]
            if frequency == 0:
                continue
            containing = document_frequencies[term]
            inverse_frequency = math.log(
                1.0
                + (len(documents) - containing + 0.5) / (containing + 0.5)
            )
            denominator = frequency + k1 * (
                1.0 - b + b * len(document) / average_length
            )
            score += inverse_frequency * frequency * (k1 + 1.0) / denominator
        scores.append(score)
    return scores


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    value = numerator / (left_norm * right_norm)
    return max(-1.0, min(1.0, value))


def _matches_filter(
    chunk: EvidenceChunk,
    metadata_filter: RAGMetadataFilter | None,
) -> bool:
    if metadata_filter is None:
        return True
    checks = (
        (metadata_filter.source_ids, chunk.source_id),
        (metadata_filter.source_types, chunk.source_type),
        (metadata_filter.pmids, chunk.pmid),
        (metadata_filter.dois, chunk.doi),
        (metadata_filter.versions, chunk.version),
        (
            metadata_filter.authorization_scopes,
            chunk.authorization_scope,
        ),
    )
    if any(allowed and actual not in allowed for allowed, actual in checks):
        return False
    return all(
        chunk.metadata.get(key) == expected
        for key, expected in metadata_filter.metadata_equals.items()
    )


class LocalRAGIndex:
    """SQLite-backed exact hybrid index stored at a caller-selected path."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.path = Path(path)
        self.embedding_provider = (
            embedding_provider or DeterministicHashEmbeddingProvider()
        )
        self._validate_provider_identity()

    def _validate_provider_identity(self) -> None:
        provider = self.embedding_provider
        if not isinstance(provider.name, str) or not provider.name.strip():
            raise ValueError("Embedding Provider 必须提供非空 name。")
        if (
            not isinstance(provider.model_name, str)
            or not provider.model_name.strip()
        ):
            raise ValueError("Embedding Provider 必须提供非空 model_name。")
        if (
            not isinstance(provider.model_version, str)
            or not provider.model_version.strip()
        ):
            raise ValueError(
                "Embedding Provider 必须提供非空 model_version。"
            )
        if (
            isinstance(provider.dimensions, bool)
            or not isinstance(provider.dimensions, int)
            or provider.dimensions < 1
        ):
            raise ValueError("Embedding Provider dimensions 必须是正整数。")
        if not isinstance(provider.fake, bool):
            raise ValueError("Embedding Provider fake 必须是布尔值。")
        if not isinstance(provider.network_used, bool):
            raise ValueError("Embedding Provider network_used 必须是布尔值。")
        if provider.network_used:
            raise ValueError("本地 RAG 禁止使用已联网的 Embedding Provider。")

    def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        if self.embedding_provider.network_used:
            raise ValueError("本地 RAG 禁止联网生成向量。")
        raw_vectors = self.embedding_provider.embed(texts)
        if self.embedding_provider.network_used:
            raise ValueError("Embedding Provider 在本地检索期间使用了网络。")
        if len(raw_vectors) != len(texts):
            raise ValueError("Embedding Provider 返回的向量数量与输入不一致。")
        vectors: list[list[float]] = []
        for raw_vector in raw_vectors:
            if len(raw_vector) != self.embedding_provider.dimensions:
                raise ValueError("Embedding Provider 返回了错误维度的向量。")
            vector = [float(value) for value in raw_vector]
            if not all(math.isfinite(value) for value in vector):
                raise ValueError("Embedding Provider 返回了非有限数值。")
            vectors.append(vector)
        return vectors

    def build(
        self,
        sources: Sequence[EvidenceSource],
        *,
        chunk_size: int = 1_200,
        overlap_chars: int = 120,
    ) -> IndexManifest:
        """Build a complete temporary database and atomically replace the target."""

        if not sources:
            raise ValueError("至少需要一个证据来源。")
        source_by_id: dict[str, EvidenceSource] = {}
        for source in sources:
            if source.source_id in source_by_id:
                raise ValueError(f"来源 ID 重复：{source.source_id}")
            source_by_id[source.source_id] = source
        ordered_sources = [source_by_id[key] for key in sorted(source_by_id)]
        chunks = sorted(
            (
                chunk
                for source in ordered_sources
                for chunk in chunk_source(
                    source,
                    chunk_size=chunk_size,
                    overlap_chars=overlap_chars,
                )
            ),
            key=lambda chunk: chunk.chunk_id,
        )
        vectors = self._embed([chunk.content for chunk in chunks])
        source_manifest = [
            source.model_dump(mode="json") for source in ordered_sources
        ]
        chunk_manifest = [chunk.model_dump(mode="json") for chunk in chunks]
        manifest = IndexManifest(
            chunk_size=chunk_size,
            overlap_chars=overlap_chars,
            source_count=len(ordered_sources),
            chunk_count=len(chunks),
            source_manifest_sha256=_sha256_text(
                _canonical_json(source_manifest)
            ),
            chunk_manifest_sha256=_sha256_text(
                _canonical_json(chunk_manifest)
            ),
            embedding_bytes_sha256=_embedding_bytes_sha256(chunks, vectors),
            embedding_provider_name=self.embedding_provider.name,
            embedding_model_name=self.embedding_provider.model_name,
            embedding_model_version=self.embedding_provider.model_version,
            embedding_dimensions=self.embedding_provider.dimensions,
            embedding_fake=bool(self.embedding_provider.fake),
        )
        self._atomic_write(ordered_sources, chunks, vectors, manifest)
        return manifest

    def _atomic_write(
        self,
        sources: Sequence[EvidenceSource],
        chunks: Sequence[EvidenceChunk],
        vectors: Sequence[Sequence[float]],
        manifest: IndexManifest,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.is_dir():
            raise IsADirectoryError(self.path)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            dir=self.path.parent,
        )
        os.close(file_descriptor)
        temporary = Path(temporary_name)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(temporary)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE manifest (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    payload TEXT NOT NULL
                );
                CREATE TABLE sources (
                    source_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL
                );
                CREATE TABLE chunks (
                    chunk_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL REFERENCES sources(source_id),
                    payload TEXT NOT NULL,
                    embedding BLOB NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT INTO manifest(singleton, payload) VALUES (1, ?)",
                (_canonical_json(manifest.model_dump(mode="json")),),
            )
            connection.executemany(
                "INSERT INTO sources(source_id, payload) VALUES (?, ?)",
                [
                    (
                        source.source_id,
                        _canonical_json(source.model_dump(mode="json")),
                    )
                    for source in sources
                ],
            )
            connection.executemany(
                """
                INSERT INTO chunks(chunk_id, source_id, payload, embedding)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        chunk.chunk_id,
                        chunk.source_id,
                        _canonical_json(chunk.model_dump(mode="json")),
                        sqlite3.Binary(_pack_vector(vector)),
                    )
                    for chunk, vector in zip(chunks, vectors, strict=True)
                ],
            )
            connection.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION}")
            connection.commit()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise ValueError("新建本地 RAG 索引未通过 SQLite 完整性检查。")
            connection.close()
            connection = None
            with temporary.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if connection is not None:
                connection.close()
            if temporary.exists():
                temporary.unlink()

    def _connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            user_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if user_version != INDEX_SCHEMA_VERSION:
                raise ValueError(
                    f"不支持的本地 RAG 索引版本：{user_version}。"
                )
        except BaseException:
            connection.close()
            raise
        return connection

    def manifest(self) -> IndexManifest:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT payload FROM manifest WHERE singleton = 1"
            ).fetchone()
        if row is None:
            raise ValueError("本地 RAG 索引缺少 manifest。")
        manifest = IndexManifest.model_validate_json(row[0])
        self._require_provider_match(manifest)
        return manifest

    def _require_provider_match(self, manifest: IndexManifest) -> None:
        actual = (
            self.embedding_provider.name,
            self.embedding_provider.model_name,
            self.embedding_provider.model_version,
            self.embedding_provider.dimensions,
            self.embedding_provider.fake,
        )
        expected = (
            manifest.embedding_provider_name,
            manifest.embedding_model_name,
            manifest.embedding_model_version,
            manifest.embedding_dimensions,
            manifest.embedding_fake,
        )
        if actual != expected:
            raise ValueError("当前 Embedding Provider 与持久化索引不匹配。")
        if self.embedding_provider.network_used:
            raise ValueError("本地 RAG 禁止使用已联网的 Embedding Provider。")

    def sources(self) -> list[EvidenceSource]:
        manifest = self.manifest()
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT payload FROM sources ORDER BY source_id"
            ).fetchall()
        sources = [EvidenceSource.model_validate_json(row[0]) for row in rows]
        if len(sources) != manifest.source_count:
            raise ValueError("来源数量与 manifest 不一致。")
        digest = _sha256_text(
            _canonical_json(
                [source.model_dump(mode="json") for source in sources]
            )
        )
        if digest != manifest.source_manifest_sha256:
            raise ValueError("来源清单哈希与 manifest 不一致。")
        return sources

    def chunks(self) -> list[EvidenceChunk]:
        manifest = self.manifest()
        sources = {source.source_id: source for source in self.sources()}
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT payload FROM chunks ORDER BY chunk_id"
            ).fetchall()
        chunks = [EvidenceChunk.model_validate_json(row[0]) for row in rows]
        if len(chunks) != manifest.chunk_count:
            raise ValueError("切片数量与 manifest 不一致。")
        chunk_digest = _sha256_text(
            _canonical_json([chunk.model_dump(mode="json") for chunk in chunks])
        )
        if chunk_digest != manifest.chunk_manifest_sha256:
            raise ValueError("切片清单哈希与 manifest 不一致。")
        for chunk in chunks:
            source = sources.get(chunk.source_id)
            if source is None or source.content_sha256 != chunk.source_content_sha256:
                raise ValueError("切片来源哈希与来源记录不一致。")
            if source.content[chunk.start_char : chunk.end_char] != chunk.content:
                raise ValueError("切片内容与来源字符位置不一致。")
        return chunks

    def search(
        self,
        query: str,
        *,
        limit: int = 3,
        metadata_filter: RAGMetadataFilter | None = None,
        keyword_weight: float = 0.5,
        vector_weight: float = 0.5,
    ) -> list[RetrievalResult]:
        clean_query = _canonical_text(query)
        if not clean_query:
            raise ValueError("检索问题不能为空。")
        if limit < 1:
            raise ValueError("limit 必须大于 0。")
        if not all(
            math.isfinite(weight) and weight >= 0.0
            for weight in (keyword_weight, vector_weight)
        ):
            raise ValueError("检索权重必须是非负有限数值。")
        total_weight = keyword_weight + vector_weight
        if total_weight == 0.0:
            raise ValueError("关键词和向量权重不能同时为 0。")

        manifest = self.manifest()
        candidates = [
            chunk
            for chunk in self.chunks()
            if _matches_filter(chunk, metadata_filter)
        ]
        if not candidates:
            return []
        keyword_scores = _bm25_scores(clean_query, candidates)
        query_vector = self._embed([clean_query])[0]
        vectors = self._read_vectors(
            [chunk.chunk_id for chunk in candidates],
            manifest,
        )
        vector_scores = [
            _cosine(query_vector, vectors[chunk.chunk_id])
            for chunk in candidates
        ]
        maximum_keyword = max(keyword_scores, default=0.0)
        normalized_keyword = [
            score / maximum_keyword if maximum_keyword > 0.0 else 0.0
            for score in keyword_scores
        ]
        normalized_keyword_weight = keyword_weight / total_weight
        normalized_vector_weight = vector_weight / total_weight
        ranked = sorted(
            (
                (
                    normalized_keyword_weight * keyword_score
                    + normalized_vector_weight * max(0.0, vector_score),
                    chunk,
                    raw_keyword,
                    vector_score,
                )
                for chunk, raw_keyword, keyword_score, vector_score in zip(
                    candidates,
                    keyword_scores,
                    normalized_keyword,
                    vector_scores,
                    strict=True,
                )
            ),
            key=lambda item: (-item[0], item[1].chunk_id),
        )[:limit]
        return [
            RetrievalResult(
                rank=rank,
                chunk=chunk,
                score=score,
                keyword_score=keyword_score,
                vector_score=vector_score,
            )
            for rank, (score, chunk, keyword_score, vector_score) in enumerate(
                ranked,
                start=1,
            )
        ]

    def _read_vectors(
        self,
        chunk_ids: Sequence[str],
        manifest: IndexManifest,
    ) -> dict[str, list[float]]:
        wanted = set(chunk_ids)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT chunk_id, embedding FROM chunks ORDER BY chunk_id"
            ).fetchall()
        embedding_digest = _embedding_rows_sha256(rows)
        if embedding_digest != manifest.embedding_bytes_sha256:
            raise ValueError("向量字节哈希与 manifest 不一致。")
        vectors = {
            chunk_id: _unpack_vector(payload, manifest.embedding_dimensions)
            for chunk_id, payload in rows
            if chunk_id in wanted
        }
        if set(vectors) != wanted:
            raise ValueError("持久化索引缺少候选切片向量。")
        return vectors


def _pack_vector(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}d", *vector)


def _unpack_vector(payload: bytes, dimension: int) -> list[float]:
    expected_size = dimension * 8
    if len(payload) != expected_size:
        raise ValueError("持久化向量字节长度与 manifest 维度不一致。")
    vector = list(struct.unpack(f"<{dimension}d", payload))
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("持久化向量包含非有限数值。")
    return vector


def _embedding_bytes_sha256(
    chunks: Sequence[EvidenceChunk],
    vectors: Sequence[Sequence[float]],
) -> str:
    rows = [
        (chunk.chunk_id, _pack_vector(vector))
        for chunk, vector in zip(chunks, vectors, strict=True)
    ]
    return _embedding_rows_sha256(rows)


def _embedding_rows_sha256(rows: Sequence[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for chunk_id, payload in rows:
        encoded_id = chunk_id.encode("utf-8")
        digest.update(struct.pack("<I", len(encoded_id)))
        digest.update(encoded_id)
        digest.update(struct.pack("<Q", len(payload)))
        digest.update(payload)
    return digest.hexdigest()


__all__ = [
    "CHUNKER_VERSION",
    "EVIDENCE_ROLE",
    "AuthorizationScope",
    "EvidenceChunk",
    "EvidenceSource",
    "IndexManifest",
    "LocalRAGIndex",
    "RAGMetadataFilter",
    "RetrievalResult",
    "chunk_source",
]
