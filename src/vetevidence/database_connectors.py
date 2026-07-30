from __future__ import annotations

import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import quote, urljoin, urlparse
from xml.sax.saxutils import escape

import httpx
from pydantic import BaseModel, ConfigDict, Field


RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
CONNECTOR_EXPORT_SCHEMA_VERSION = "vetevidence-connector-result-v2"
CONNECTOR_PARSER_VERSION = "vetevidence-database-connectors-0.5"
SENSITIVE_KEYS = frozenset(
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


class ConnectorError(RuntimeError):
    """Base error for a connector transport or response failure."""


class ConnectorTransportError(ConnectorError):
    """Raised after all retry attempts for a transport error are exhausted."""


class ConnectorStatus(StrEnum):
    OK = "ok"
    NO_RESULTS = "no_results"
    DEGRADED = "degraded"
    OFFLINE_EXPORT = "offline_export"


class AcquisitionMode(StrEnum):
    ONLINE_API = "online_api"
    MANUAL_IMPORT = "manual_import"
    OFFLINE_REQUEST = "offline_request"


class DatabaseEvidenceClass(StrEnum):
    CURATED_DATABASE = "curated_database"
    COMPUTATIONAL_PREDICTION = "computational_prediction"


class ConnectorModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class IdentifierCandidate(ConnectorModel):
    identifier: str = Field(min_length=1)
    label: str | None = None
    taxon_id: int | None = Field(default=None, ge=1)


class IdentifierMapping(ConnectorModel):
    input_identifier: str = Field(min_length=1)
    namespace: str = Field(min_length=1)
    canonical_identifier: str | None = None
    candidates: tuple[IdentifierCandidate, ...] = ()
    ambiguous: bool = False
    mapping_method: str = Field(min_length=1)
    taxon_id: int | None = Field(default=None, ge=1)
    warning: str | None = None


class ProvenanceRecord(ConnectorModel):
    source_name: str = Field(min_length=1)
    endpoint_url: str = Field(min_length=1)
    method: Literal["GET", "POST", "IMPORT"]
    normalized_request: str = Field(min_length=1)
    request_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    raw_response_sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    retrieved_at_utc: datetime
    http_status: int | None = Field(default=None, ge=100, le=599)
    content_type: str | None = None
    source_version: str | None = None
    source_release_date: str | None = None
    stable_ids: tuple[str, ...] = ()
    license_url: str | None = None
    citation_url: str | None = None


class ResponseArtifact(ConnectorModel):
    provenance: ProvenanceRecord
    raw_response: bytes = Field(repr=False, exclude=True)
    response_headers: dict[str, str] = Field(default_factory=dict, exclude=True)


class OfflineRequest(ConnectorModel):
    media_type: str = Field(min_length=1)
    content: str = Field(min_length=1)
    sha256: str = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )


class ConnectorResult(ConnectorModel):
    status: ConnectorStatus
    acquisition_mode: AcquisitionMode = AcquisitionMode.ONLINE_API
    evidence_class: DatabaseEvidenceClass = (
        DatabaseEvidenceClass.CURATED_DATABASE
    )
    records: tuple[dict[str, Any], ...] = ()
    artifacts: tuple[ResponseArtifact, ...] = ()
    mappings: tuple[IdentifierMapping, ...] = ()
    warnings: tuple[str, ...] = ()
    offline_request: OfflineRequest | None = None

    @property
    def provenance(self) -> tuple[ProvenanceRecord, ...]:
        return tuple(artifact.provenance for artifact in self.artifacts)


def canonical_json(value: Any) -> str:
    """Return stable JSON used for request and offline-manifest hashing."""

    return json.dumps(
        _jsonable(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, bytes):
        return {
            "content_length": len(value),
            "content_sha256": sha256_bytes(value),
        }
    if isinstance(value, StrEnum):
        return value.value
    return value


def _redact(value: Any, *, parent_key: str | None = None) -> Any:
    if parent_key and parent_key.casefold() in SENSITIVE_KEYS:
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(key): _redact(item, parent_key=str(key))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, parent_key=parent_key) for item in value]
    return value


def normalized_request(
    method: str,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    data: Mapping[str, Any] | None = None,
    json_body: Any = None,
    content: bytes | str | None = None,
) -> str:
    body: Any = None
    if data is not None:
        body = _redact(data)
    elif json_body is not None:
        body = _redact(json_body)
    elif content is not None:
        raw = content.encode("utf-8") if isinstance(content, str) else content
        body = {
            "content_length": len(raw),
            "content_sha256": sha256_bytes(raw),
        }
    return canonical_json(
        {
            "body": body,
            "method": method.upper(),
            "params": _redact(params or {}),
            "url": url,
        }
    )


class RequestExecutor:
    """Small synchronous HTTP executor with injectable time and retry behavior."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        min_interval_seconds: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative.")
        if retry_backoff_seconds < 0 or min_interval_seconds < 0:
            raise ValueError("Retry and rate-limit intervals must be non-negative.")
        self._client = client
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.min_interval_seconds = min_interval_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._utcnow = utcnow or (lambda: datetime.now(UTC))
        self._last_started_at: float | None = None
        self.request_count = 0

    def _wait_for_rate_limit(self) -> None:
        now = self._monotonic()
        if self._last_started_at is not None:
            remaining = (
                self.min_interval_seconds - (now - self._last_started_at)
            )
            if remaining > 0:
                self._sleep(remaining)
        self._last_started_at = self._monotonic()

    def request(
        self,
        source_name: str,
        method: Literal["GET", "POST"],
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        json_body: Any = None,
        content: bytes | str | None = None,
        headers: Mapping[str, str] | None = None,
        license_url: str | None = None,
        citation_url: str | None = None,
    ) -> ResponseArtifact:
        normalized = normalized_request(
            method,
            url,
            params=params,
            data=data,
            json_body=json_body,
            content=content,
        )
        request_digest = sha256_bytes(normalized.encode("utf-8"))
        attempts = self.max_retries + 1
        last_error: httpx.RequestError | None = None

        for attempt in range(attempts):
            response: httpx.Response | None = None
            self._wait_for_rate_limit()
            self.request_count += 1
            try:
                response = self._client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    json=json_body,
                    content=content,
                    headers=headers,
                )
            except httpx.RequestError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise ConnectorTransportError(
                        f"{source_name} request failed after {attempts} attempts."
                    ) from exc
            else:
                if (
                    response.status_code not in RETRYABLE_STATUS_CODES
                    or attempt + 1 >= attempts
                ):
                    raw = response.content
                    provenance = ProvenanceRecord(
                        source_name=source_name,
                        endpoint_url=url,
                        method=method,
                        normalized_request=normalized,
                        request_sha256=request_digest,
                        raw_response_sha256=sha256_bytes(raw),
                        retrieved_at_utc=self._utcnow(),
                        http_status=response.status_code,
                        content_type=response.headers.get("content-type"),
                        source_version=response.headers.get(
                            "x-uniprot-release"
                        ),
                        source_release_date=response.headers.get(
                            "x-uniprot-release-date"
                        ),
                        license_url=license_url,
                        citation_url=citation_url,
                    )
                    return ResponseArtifact(
                        provenance=provenance,
                        raw_response=raw,
                        response_headers={
                            key.casefold(): value
                            for key, value in response.headers.items()
                            if key.casefold()
                            in {
                                "content-type",
                                "etag",
                                "last-modified",
                                "link",
                                "location",
                                "retry-after",
                                "x-uniprot-release",
                                "x-uniprot-release-date",
                                "x-per-page",
                                "x-total-count",
                            }
                        },
                    )

            delay = self.retry_backoff_seconds * (2**attempt)
            if response is not None:
                retry_after = response.headers.get("retry-after")
                if retry_after:
                    try:
                        delay = max(delay, float(retry_after))
                    except ValueError:
                        pass
            self._sleep(delay)

        if last_error is not None:
            raise ConnectorTransportError(
                f"{source_name} request failed."
            ) from last_error
        raise ConnectorTransportError(
            f"{source_name} request loop exited unexpectedly."
        )


class BaseConnector:
    source_name = "external_database"
    license_url: str | None = None
    citation_url: str | None = None
    default_min_interval_seconds = 0.0

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 20.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        min_interval_seconds: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] | None = None,
        user_agent: str = "VetEvidence-Agent/0.4",
    ) -> None:
        if client is not None and transport is not None:
            raise ValueError("Pass either client or transport, not both.")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            transport=transport,
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )
        self._executor = RequestExecutor(
            self._client,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            min_interval_seconds=(
                self.default_min_interval_seconds
                if min_interval_seconds is None
                else min_interval_seconds
            ),
            sleep=sleep,
            monotonic=monotonic,
            utcnow=utcnow,
        )

    @property
    def request_count(self) -> int:
        return self._executor.request_count

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> BaseConnector:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(
        self,
        method: Literal["GET", "POST"],
        url: str,
        **kwargs: Any,
    ) -> ResponseArtifact:
        return self._executor.request(
            self.source_name,
            method,
            url,
            license_url=self.license_url,
            citation_url=self.citation_url,
            **kwargs,
        )

    @staticmethod
    def _http_failure(
        artifact: ResponseArtifact,
        *,
        no_result_statuses: Iterable[int] = (204, 404),
    ) -> ConnectorResult | None:
        status = artifact.provenance.http_status
        if status in set(no_result_statuses):
            return ConnectorResult(
                status=ConnectorStatus.NO_RESULTS,
                artifacts=(artifact,),
                warnings=(f"No record was returned (HTTP {status}).",),
            )
        if status >= 400:
            return ConnectorResult(
                status=ConnectorStatus.DEGRADED,
                artifacts=(artifact,),
                warnings=(f"Remote service returned HTTP {status}.",),
            )
        return None


def _with_provenance(
    artifact: ResponseArtifact,
    *,
    source_version: str | None = None,
    source_release_date: str | None = None,
    stable_ids: Sequence[str] = (),
) -> ResponseArtifact:
    updates: dict[str, Any] = {
        "stable_ids": tuple(dict.fromkeys(stable_ids)),
    }
    if source_version is not None:
        updates["source_version"] = source_version
    if source_release_date is not None:
        updates["source_release_date"] = source_release_date
    provenance = artifact.provenance.model_copy(update=updates)
    return artifact.model_copy(update={"provenance": provenance})


def _json_payload(artifact: ResponseArtifact) -> Any:
    try:
        return json.loads(artifact.raw_response)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConnectorError(
            f"{artifact.provenance.source_name} returned invalid JSON."
        ) from exc


def _clean_identifier(value: str, label: str = "identifier") -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} cannot be blank.")
    return cleaned


def _pubchem_date(value: Any) -> str | None:
    if isinstance(value, Mapping):
        try:
            year = int(value["Year"])
            month = int(value["Month"])
            day = int(value["Day"])
        except (KeyError, TypeError, ValueError):
            return None
        try:
            return datetime(year, month, day, tzinfo=UTC).date().isoformat()
        except ValueError:
            return None
    if value in (None, ""):
        return None
    return str(value)


class PubChemConnector(BaseConnector):
    source_name = "PubChem"
    base_url = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
    license_url = "https://pubchem.ncbi.nlm.nih.gov/docs/downloads"
    citation_url = "https://pubchem.ncbi.nlm.nih.gov/docs/citation-guidelines"
    default_min_interval_seconds = 0.2

    def fetch_compound(
        self,
        identifier: str,
        *,
        namespace: Literal["cid", "name", "inchikey"] = "name",
        include_3d: bool = False,
    ) -> ConnectorResult:
        cleaned = _clean_identifier(identifier, "compound identifier")
        artifacts: list[ResponseArtifact] = []
        mappings: list[IdentifierMapping] = []
        warnings: list[str] = []

        if namespace == "cid":
            if not cleaned.isdigit():
                raise ValueError("PubChem CID must be numeric.")
            cid = cleaned
        else:
            lookup_url = (
                f"{self.base_url}/compound/{namespace}/"
                f"{quote(cleaned, safe='')}/cids/JSON"
            )
            lookup = self._request("GET", lookup_url)
            artifacts.append(lookup)
            failure = self._http_failure(lookup)
            if failure is not None:
                return failure.model_copy(
                    update={
                        "mappings": (
                            IdentifierMapping(
                                input_identifier=cleaned,
                                namespace=namespace,
                                mapping_method="pubchem_identity_lookup",
                                warning="No PubChem CID could be resolved.",
                            ),
                        )
                    }
                )
            payload = _json_payload(lookup)
            cids = [
                str(value)
                for value in payload.get("IdentifierList", {}).get("CID", [])
            ]
            candidates = tuple(
                IdentifierCandidate(identifier=f"PubChem:CID:{cid_value}")
                for cid_value in cids
            )
            if not cids:
                return ConnectorResult(
                    status=ConnectorStatus.NO_RESULTS,
                    artifacts=tuple(artifacts),
                    mappings=(
                        IdentifierMapping(
                            input_identifier=cleaned,
                            namespace=namespace,
                            mapping_method="pubchem_identity_lookup",
                            warning="No PubChem CID could be resolved.",
                        ),
                    ),
                )
            if len(cids) > 1:
                return ConnectorResult(
                    status=ConnectorStatus.DEGRADED,
                    artifacts=tuple(artifacts),
                    mappings=(
                        IdentifierMapping(
                            input_identifier=cleaned,
                            namespace=namespace,
                            candidates=candidates,
                            ambiguous=True,
                            mapping_method="pubchem_identity_lookup",
                            warning=(
                                "The identifier maps to multiple PubChem CIDs; "
                                "explicit user selection is required."
                            ),
                        ),
                    ),
                    warnings=(
                        "Ambiguous compound identity; no compound was selected.",
                    ),
                )
            cid = cids[0]
            mappings.append(
                IdentifierMapping(
                    input_identifier=cleaned,
                    namespace=namespace,
                    canonical_identifier=f"PubChem:CID:{cid}",
                    candidates=candidates,
                    mapping_method="pubchem_identity_lookup",
                )
            )

        properties = (
            "IUPACName,SMILES,ConnectivitySMILES,InChI,InChIKey,"
            "MolecularFormula,MolecularWeight"
        )
        property_url = (
            f"{self.base_url}/compound/cid/{cid}/property/{properties}/JSON"
        )
        property_artifact = self._request("GET", property_url)
        artifacts.append(property_artifact)
        failure = self._http_failure(property_artifact)
        if failure is not None:
            return failure.model_copy(
                update={
                    "artifacts": tuple(artifacts),
                    "mappings": tuple(mappings),
                }
            )
        property_payload = _json_payload(property_artifact)
        rows = property_payload.get("PropertyTable", {}).get("Properties", [])
        if not rows:
            return ConnectorResult(
                status=ConnectorStatus.NO_RESULTS,
                artifacts=tuple(artifacts),
                mappings=tuple(mappings),
                warnings=("PubChem returned no property record.",),
            )
        raw_record = dict(rows[0])

        dates_url = f"{self.base_url}/compound/cid/{cid}/dates/JSON"
        dates_artifact = self._request("GET", dates_url)
        artifacts.append(dates_artifact)
        dates: dict[str, Any] = {}
        if dates_artifact.provenance.http_status < 400:
            dates_payload = _json_payload(dates_artifact)
            date_rows = dates_payload.get("InformationList", {}).get(
                "Information", []
            )
            if date_rows:
                dates = dict(date_rows[0])
        else:
            warnings.append(
                "PubChem record dates were unavailable; the chemical "
                "properties remain usable but the source snapshot is partial."
            )

        inchikey = raw_record.get("InChIKey")
        stable_ids = [f"PubChem:CID:{cid}"]
        if inchikey:
            stable_ids.append(f"InChIKey:{inchikey}")
        create_date = _pubchem_date(
            dates.get("CreationDate")
            or dates.get("CreateDate")
            or dates.get("Create Date")
            or dates.get("create_date")
        )
        modify_date = _pubchem_date(
            dates.get("ModificationDate")
            or dates.get("ModifyDate")
            or dates.get("Modify Date")
            or dates.get("modify_date")
        )
        record_version = (
            f"record-modified:{modify_date}" if modify_date else None
        )
        artifacts = [
            _with_provenance(
                artifact,
                source_version=record_version,
                stable_ids=stable_ids,
            )
            for artifact in artifacts
        ]
        record: dict[str, Any] = {
            "record_type": "compound",
            "cid": int(cid),
            "stable_ids": stable_ids,
            "iupac_name": raw_record.get("IUPACName"),
            "canonical_smiles": (
                raw_record.get("ConnectivitySMILES")
                or raw_record.get("CanonicalSMILES")
            ),
            "isomeric_smiles": (
                raw_record.get("SMILES")
                or raw_record.get("IsomericSMILES")
            ),
            "inchi": raw_record.get("InChI"),
            "inchikey": inchikey,
            "molecular_formula": raw_record.get("MolecularFormula"),
            "molecular_weight": raw_record.get("MolecularWeight"),
            "create_date": create_date,
            "modify_date": modify_date,
            "source_url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
        }

        if include_3d:
            structure_url = (
                f"{self.base_url}/compound/cid/{cid}/record/SDF"
                "?record_type=3d"
            )
            structure_artifact = self._request("GET", structure_url)
            artifacts.append(
                _with_provenance(
                    structure_artifact,
                    source_version=(
                        record_version
                    ),
                    stable_ids=stable_ids,
                )
            )
            if structure_artifact.provenance.http_status < 400:
                record["structure_3d_sdf_sha256"] = (
                    structure_artifact.provenance.raw_response_sha256
                )
            else:
                warnings.append(
                    "PubChem 3D conformer was unavailable; properties remain usable."
                )

        return ConnectorResult(
            status=(
                ConnectorStatus.DEGRADED
                if warnings
                else ConnectorStatus.OK
            ),
            records=(record,),
            artifacts=tuple(artifacts),
            mappings=tuple(mappings),
            warnings=tuple(warnings),
        )


class UniProtConnector(BaseConnector):
    source_name = "UniProt"
    base_url = "https://rest.uniprot.org"
    license_url = "https://www.uniprot.org/help/license"
    citation_url = "https://www.uniprot.org/help/publications"

    def fetch_protein(
        self,
        accession: str,
        *,
        taxon_id: int | None = None,
    ) -> ConnectorResult:
        cleaned = _clean_identifier(accession, "UniProt accession").upper()
        if taxon_id is not None and taxon_id < 1:
            raise ValueError("taxon_id must be a positive NCBI TaxID.")
        url = f"{self.base_url}/uniprotkb/{quote(cleaned, safe='')}"
        artifact = self._request(
            "GET",
            url,
            params={"format": "json"},
        )
        artifacts = [artifact]

        if artifact.provenance.http_status == 303:
            location = artifact.response_headers.get("location")
            if location:
                redirected = self._request("GET", urljoin(url, location))
                artifacts.append(redirected)
                artifact = redirected

        failure = self._http_failure(artifact)
        if failure is not None:
            return failure.model_copy(update={"artifacts": tuple(artifacts)})
        payload = _json_payload(artifact)
        if not payload.get("primaryAccession"):
            return ConnectorResult(
                status=ConnectorStatus.NO_RESULTS,
                artifacts=tuple(artifacts),
                warnings=("UniProt returned no protein entry.",),
            )
        primary = str(payload.get("primaryAccession") or cleaned)
        organism = payload.get("organism") or {}
        actual_taxon = organism.get("taxonId")
        if (
            taxon_id is not None
            and actual_taxon is not None
            and int(actual_taxon) != taxon_id
        ):
            mapping = IdentifierMapping(
                input_identifier=cleaned,
                namespace="UniProtKB",
                canonical_identifier=primary,
                candidates=(
                    IdentifierCandidate(
                        identifier=primary,
                        taxon_id=int(actual_taxon),
                    ),
                ),
                mapping_method="uniprot_primary_accession",
                taxon_id=taxon_id,
                warning="The returned protein belongs to a different TaxID.",
            )
            return ConnectorResult(
                status=ConnectorStatus.DEGRADED,
                artifacts=tuple(artifacts),
                mappings=(mapping,),
                warnings=(
                    f"TaxID mismatch: requested {taxon_id}, "
                    f"returned {actual_taxon}.",
                ),
            )

        audit = payload.get("entryAudit") or {}
        sequence = payload.get("sequence") or {}
        release_number = (
            artifact.provenance.source_version
            or _extract_json_release(payload)
        )
        release_date = (
            artifact.provenance.source_release_date
            or audit.get("lastAnnotationUpdateDate")
        )
        stable_ids = [f"UniProtKB:{primary}"]
        if actual_taxon:
            stable_ids.append(f"NCBITaxon:{actual_taxon}")
        artifacts = [
            _with_provenance(
                item,
                source_version=release_number,
                source_release_date=(
                    str(release_date) if release_date else None
                ),
                stable_ids=stable_ids,
            )
            for item in artifacts
        ]

        protein_description = payload.get("proteinDescription") or {}
        recommended = (
            protein_description.get("recommendedName", {})
            .get("fullName", {})
            .get("value")
        )
        genes = payload.get("genes") or []
        gene_names = [
            gene.get("geneName", {}).get("value")
            for gene in genes
            if gene.get("geneName", {}).get("value")
        ]
        xrefs: dict[str, list[str]] = defaultdict(list)
        for xref in payload.get("uniProtKBCrossReferences") or []:
            database = str(xref.get("database") or "")
            identifier = xref.get("id")
            if database and identifier and database in {
                "PDB",
                "RefSeq",
                "GeneID",
                "EMBL",
            }:
                xrefs[database].append(str(identifier))
        mapping = IdentifierMapping(
            input_identifier=cleaned,
            namespace="UniProtKB",
            canonical_identifier=primary,
            candidates=(
                IdentifierCandidate(
                    identifier=primary,
                    label=recommended,
                    taxon_id=int(actual_taxon) if actual_taxon else None,
                ),
            ),
            mapping_method="uniprot_primary_accession",
            taxon_id=taxon_id,
            warning=(
                "Inactive or secondary accession resolved to a primary accession."
                if primary != cleaned
                else None
            ),
        )
        record = {
            "record_type": "protein",
            "primary_accession": primary,
            "entry_type": payload.get("entryType"),
            "reviewed": "reviewed" in str(payload.get("entryType", "")).lower(),
            "protein_name": recommended,
            "gene_names": gene_names,
            "taxon_id": int(actual_taxon) if actual_taxon else None,
            "organism_name": organism.get("scientificName"),
            "sequence": sequence.get("value"),
            "sequence_length": sequence.get("length"),
            "sequence_version": audit.get("sequenceVersion"),
            "entry_version": audit.get("entryVersion"),
            "last_annotation_update": release_date,
            "cross_references": dict(xrefs),
            "source_url": f"https://www.uniprot.org/uniprotkb/{primary}",
        }
        warnings = (
            (mapping.warning,) if mapping.warning is not None else ()
        )
        return ConnectorResult(
            status=ConnectorStatus.OK,
            records=(record,),
            artifacts=tuple(artifacts),
            mappings=(mapping,),
            warnings=warnings,
        )


def _extract_json_release(payload: Mapping[str, Any]) -> str | None:
    audit = payload.get("entryAudit")
    if isinstance(audit, Mapping):
        entry_version = audit.get("entryVersion")
        sequence_version = audit.get("sequenceVersion")
        if entry_version is not None or sequence_version is not None:
            return (
                f"entry:{entry_version or 'unknown'};"
                f"sequence:{sequence_version or 'unknown'}"
            )
    return None


class NCBIConnector(BaseConnector):
    source_name = "NCBI Gene/GenBank"
    eutils_base = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
    license_url = "https://www.ncbi.nlm.nih.gov/home/about/policies/"
    citation_url = "https://www.ncbi.nlm.nih.gov/home/about/citation/"

    def __init__(
        self,
        *,
        tool: str = "vetevidence_agent",
        email: str | None = None,
        api_key: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.tool = _clean_identifier(tool, "NCBI tool")
        self.email = email.strip() if email else None
        self.api_key = api_key.strip() if api_key else None
        kwargs.setdefault(
            "min_interval_seconds",
            0.1 if self.api_key else (1 / 3),
        )
        super().__init__(**kwargs)

    def _common_params(self) -> dict[str, Any]:
        params: dict[str, Any] = {"tool": self.tool}
        if self.email:
            params["email"] = self.email
        if self.api_key:
            params["api_key"] = self.api_key
        return params

    def _missing_contact_result(
        self,
        operation: str,
        payload: Mapping[str, Any],
    ) -> ConnectorResult | None:
        if self.email:
            return None
        content = canonical_json(
            {
                "operation": operation,
                "parameters": payload,
                "required_configuration": "NCBI_EMAIL",
                "schema_version": "vetevidence-ncbi-request-v1",
            }
        )
        return ConnectorResult(
            status=ConnectorStatus.OFFLINE_EXPORT,
            acquisition_mode=AcquisitionMode.OFFLINE_REQUEST,
            offline_request=OfflineRequest(
                media_type="application/vnd.vetevidence.ncbi-request+json",
                content=content,
                sha256=sha256_bytes(content.encode("utf-8")),
            ),
            warnings=(
                "NCBI_EMAIL is not configured, so VetEvidence did not send "
                "the request. Configure a contact email and retry.",
            ),
        )

    def fetch_gene(
        self,
        identifier: str,
        *,
        taxon_id: int,
        identifier_type: Literal["gene_id", "symbol"] = "gene_id",
    ) -> ConnectorResult:
        cleaned = _clean_identifier(identifier, "gene identifier")
        if taxon_id < 1:
            raise ValueError("taxon_id must be a positive NCBI TaxID.")
        missing_contact = self._missing_contact_result(
            "fetch_gene",
            {
                "identifier": cleaned,
                "identifier_type": identifier_type,
                "taxon_id": taxon_id,
            },
        )
        if missing_contact is not None:
            return missing_contact
        artifacts: list[ResponseArtifact] = []
        mappings: list[IdentifierMapping] = []
        warnings: list[str] = []

        if identifier_type == "symbol":
            search_url = f"{self.eutils_base}/esearch.fcgi"
            term = f"{cleaned}[sym] AND txid{taxon_id}[Organism:exp]"
            search = self._request(
                "GET",
                search_url,
                params={
                    "db": "gene",
                    "term": term,
                    "retmode": "json",
                    "retmax": 20,
                    **self._common_params(),
                },
            )
            artifacts.append(search)
            failure = self._http_failure(search)
            if failure is not None:
                return failure.model_copy(
                    update={"warnings": tuple(warnings) + failure.warnings}
                )
            payload = _json_payload(search)
            gene_ids = [
                str(value)
                for value in payload.get("esearchresult", {}).get("idlist", [])
            ]
            candidates = tuple(
                IdentifierCandidate(
                    identifier=f"NCBIGene:{value}",
                    taxon_id=taxon_id,
                )
                for value in gene_ids
            )
            if not gene_ids:
                return ConnectorResult(
                    status=ConnectorStatus.NO_RESULTS,
                    artifacts=tuple(artifacts),
                    mappings=(
                        IdentifierMapping(
                            input_identifier=cleaned,
                            namespace="gene_symbol",
                            candidates=(),
                            mapping_method="ncbi_gene_symbol_search",
                            taxon_id=taxon_id,
                            warning="No GeneID matched the symbol and TaxID.",
                        ),
                    ),
                    warnings=tuple(warnings),
                )
            if len(gene_ids) > 1:
                return ConnectorResult(
                    status=ConnectorStatus.DEGRADED,
                    artifacts=tuple(artifacts),
                    mappings=(
                        IdentifierMapping(
                            input_identifier=cleaned,
                            namespace="gene_symbol",
                            candidates=candidates,
                            ambiguous=True,
                            mapping_method="ncbi_gene_symbol_search",
                            taxon_id=taxon_id,
                            warning=(
                                "The gene symbol maps to multiple GeneIDs; "
                                "user selection is required."
                            ),
                        ),
                    ),
                    warnings=tuple(warnings)
                    + ("Ambiguous NCBI Gene mapping.",),
                )
            gene_id = gene_ids[0]
            mappings.append(
                IdentifierMapping(
                    input_identifier=cleaned,
                    namespace="gene_symbol",
                    canonical_identifier=f"NCBIGene:{gene_id}",
                    candidates=candidates,
                    mapping_method="ncbi_gene_symbol_search",
                    taxon_id=taxon_id,
                )
            )
        else:
            if not cleaned.isdigit():
                raise ValueError("NCBI GeneID must be numeric.")
            gene_id = cleaned
            mappings.append(
                IdentifierMapping(
                    input_identifier=cleaned,
                    namespace="NCBIGene",
                    canonical_identifier=f"NCBIGene:{gene_id}",
                    candidates=(
                        IdentifierCandidate(
                            identifier=f"NCBIGene:{gene_id}",
                            taxon_id=taxon_id,
                        ),
                    ),
                    mapping_method="ncbi_gene_id",
                    taxon_id=taxon_id,
                )
            )

        summary_url = f"{self.eutils_base}/esummary.fcgi"
        summary = self._request(
            "GET",
            summary_url,
            params={
                "db": "gene",
                "id": gene_id,
                "retmode": "json",
                **self._common_params(),
            },
        )
        artifacts.append(summary)
        failure = self._http_failure(summary)
        if failure is not None:
            return failure.model_copy(
                update={
                    "artifacts": tuple(artifacts),
                    "mappings": tuple(mappings),
                    "warnings": tuple(warnings) + failure.warnings,
                }
            )
        payload = _json_payload(summary)
        record_payload = payload.get("result", {}).get(gene_id)
        if not isinstance(record_payload, Mapping):
            return ConnectorResult(
                status=ConnectorStatus.NO_RESULTS,
                artifacts=tuple(artifacts),
                mappings=tuple(mappings),
                warnings=tuple(warnings)
                + ("NCBI returned no Gene summary record.",),
            )
        organism = record_payload.get("organism") or {}
        actual_taxon = organism.get("taxid")
        if actual_taxon is not None and int(actual_taxon) != taxon_id:
            return ConnectorResult(
                status=ConnectorStatus.DEGRADED,
                artifacts=tuple(artifacts),
                mappings=tuple(mappings),
                warnings=tuple(warnings)
                + (
                    f"TaxID mismatch: requested {taxon_id}, "
                    f"returned {actual_taxon}.",
                ),
            )
        current_id = str(record_payload.get("currentid") or "").strip()
        canonical_gene_id = current_id if current_id not in {"", "0"} else gene_id
        update_date = record_payload.get("updatedate")
        stable_ids = [
            f"NCBIGene:{canonical_gene_id}",
            f"NCBITaxon:{taxon_id}",
        ]
        artifacts = [
            _with_provenance(
                item,
                source_version=str(update_date) if update_date else None,
                source_release_date=(
                    str(update_date) if update_date else None
                ),
                stable_ids=stable_ids,
            )
            for item in artifacts
        ]
        if canonical_gene_id != gene_id:
            mappings.append(
                IdentifierMapping(
                    input_identifier=f"NCBIGene:{gene_id}",
                    namespace="NCBIGene",
                    canonical_identifier=f"NCBIGene:{canonical_gene_id}",
                    candidates=(
                        IdentifierCandidate(
                            identifier=f"NCBIGene:{canonical_gene_id}",
                            taxon_id=taxon_id,
                        ),
                    ),
                    mapping_method="ncbi_gene_replacement",
                    taxon_id=taxon_id,
                    warning="The requested GeneID was replaced or merged.",
                )
            )
            warnings.append("NCBI GeneID was replaced or merged.")
        record = {
            "record_type": "gene",
            "gene_id": canonical_gene_id,
            "requested_gene_id": gene_id,
            "symbol": record_payload.get("name"),
            "description": record_payload.get("description"),
            "aliases": _split_aliases(record_payload.get("otheraliases")),
            "status": record_payload.get("status"),
            "taxon_id": taxon_id,
            "organism_name": organism.get("scientificname"),
            "chromosome": record_payload.get("chromosome"),
            "map_location": record_payload.get("maplocation"),
            "updated_at": update_date,
            "source_url": (
                f"https://www.ncbi.nlm.nih.gov/gene/{canonical_gene_id}"
            ),
        }
        return ConnectorResult(
            status=(
                ConnectorStatus.DEGRADED
                if warnings
                else ConnectorStatus.OK
            ),
            records=(record,),
            artifacts=tuple(artifacts),
            mappings=tuple(mappings),
            warnings=tuple(warnings),
        )

    def fetch_nucleotide(
        self,
        accession_version: str,
        *,
        taxon_id: int | None = None,
    ) -> ConnectorResult:
        accession = _clean_identifier(
            accession_version,
            "GenBank accession.version",
        )
        if taxon_id is not None and taxon_id < 1:
            raise ValueError("taxon_id must be a positive NCBI TaxID.")
        missing_contact = self._missing_contact_result(
            "fetch_nucleotide",
            {
                "accession_version": accession,
                "taxon_id": taxon_id,
            },
        )
        if missing_contact is not None:
            return missing_contact
        artifact = self._request(
            "GET",
            f"{self.eutils_base}/efetch.fcgi",
            params={
                "db": "nuccore",
                "id": accession,
                "rettype": "gbwithparts",
                "retmode": "text",
                **self._common_params(),
            },
        )
        failure = self._http_failure(artifact)
        if failure is not None:
            return failure
        text = artifact.raw_response.decode("utf-8", errors="replace")
        if not text.strip() or text.lstrip().startswith("Error"):
            return ConnectorResult(
                status=ConnectorStatus.NO_RESULTS,
                artifacts=(artifact,),
                warnings=("No GenBank nucleotide record was returned.",),
            )
        version_match = re.search(r"^VERSION\s+(\S+)", text, re.MULTILINE)
        accession_match = re.search(r"^ACCESSION\s+(\S+)", text, re.MULTILINE)
        organism_match = re.search(r"^\s+ORGANISM\s+(.+)$", text, re.MULTILINE)
        taxon_match = re.search(r'/db_xref="taxon:(\d+)"', text)
        version = version_match.group(1) if version_match else accession
        base_accession = (
            accession_match.group(1)
            if accession_match
            else version.split(".", 1)[0]
        )
        actual_taxon = int(taxon_match.group(1)) if taxon_match else None
        stable_ids = [f"GenBank:{version}"]
        if actual_taxon:
            stable_ids.append(f"NCBITaxon:{actual_taxon}")
        artifact = _with_provenance(
            artifact,
            source_version=version,
            stable_ids=stable_ids,
        )
        if (
            taxon_id is not None
            and actual_taxon is not None
            and actual_taxon != taxon_id
        ):
            return ConnectorResult(
                status=ConnectorStatus.DEGRADED,
                artifacts=(artifact,),
                warnings=(
                    f"TaxID mismatch: requested {taxon_id}, "
                    f"returned {actual_taxon}.",
                ),
            )
        warnings: list[str] = []
        if "." not in version:
            warnings.append(
                "The response did not expose accession.version; exact sequence "
                "revision cannot be pinned."
            )
        record = {
            "record_type": "nucleotide",
            "accession": base_accession,
            "accession_version": version,
            "taxon_id": actual_taxon,
            "organism_name": (
                organism_match.group(1).strip() if organism_match else None
            ),
            "source_url": f"https://www.ncbi.nlm.nih.gov/nuccore/{version}",
            "genbank_flatfile_sha256": (
                artifact.provenance.raw_response_sha256
            ),
        }
        return ConnectorResult(
            status=(
                ConnectorStatus.DEGRADED
                if warnings
                else ConnectorStatus.OK
            ),
            records=(record,),
            artifacts=(artifact,),
            warnings=tuple(warnings),
        )


def _split_aliases(value: Any) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in str(value).split(",") if item.strip()]


class RCSBConnector(BaseConnector):
    source_name = "RCSB PDB"
    search_url = "https://search.rcsb.org/rcsbsearch/v2/query"
    data_base = "https://data.rcsb.org/rest/v1/core"
    files_base = "https://files.rcsb.org/download"
    license_url = "https://www.rcsb.org/pages/policies"
    citation_url = "https://www.rcsb.org/pages/policies"

    def search_structures(
        self,
        uniprot_accession: str,
        *,
        taxon_id: int,
        experimental_only: bool = True,
        max_results: int = 25,
    ) -> ConnectorResult:
        accession = _clean_identifier(
            uniprot_accession,
            "UniProt accession",
        ).upper()
        if taxon_id < 1:
            raise ValueError("taxon_id must be a positive NCBI TaxID.")
        if not 1 <= max_results <= 1000:
            raise ValueError("max_results must be between 1 and 1000.")
        query = {
            "query": {
                "type": "group",
                "logical_operator": "and",
                "nodes": [
                    {
                        "type": "terminal",
                        "service": "text",
                        "parameters": {
                            "attribute": (
                                "rcsb_polymer_entity_container_identifiers."
                                "reference_sequence_identifiers."
                                "database_accession"
                            ),
                            "operator": "exact_match",
                            "value": accession,
                        },
                    },
                    {
                        "type": "terminal",
                        "service": "text",
                        "parameters": {
                            "attribute": (
                                "rcsb_entity_source_organism."
                                "taxonomy_lineage.id"
                            ),
                            "operator": "in",
                            "value": [str(taxon_id)],
                        },
                    },
                ],
            },
            "request_options": {
                "paginate": {"start": 0, "rows": max_results},
                "results_verbosity": "compact",
            },
            "return_type": "entry",
        }
        if experimental_only:
            query["request_options"]["results_content_type"] = ["experimental"]
        artifact = self._request("POST", self.search_url, json_body=query)
        failure = self._http_failure(artifact)
        if failure is not None:
            return failure
        payload = _json_payload(artifact)
        result_set = payload.get("result_set") or []
        pdb_ids: list[str] = []
        for item in result_set:
            identifier = (
                item if isinstance(item, str) else item.get("identifier")
            )
            if identifier:
                pdb_ids.append(str(identifier))
        stable_ids = [
            f"UniProtKB:{accession}",
            f"NCBITaxon:{taxon_id}",
            *(f"PDB:{pdb_id}" for pdb_id in pdb_ids),
        ]
        artifact = _with_provenance(artifact, stable_ids=stable_ids)
        mapping = IdentifierMapping(
            input_identifier=accession,
            namespace="UniProtKB",
            canonical_identifier=accession,
            candidates=tuple(
                IdentifierCandidate(
                    identifier=f"PDB:{pdb_id}",
                    taxon_id=taxon_id,
                )
                for pdb_id in pdb_ids
            ),
            mapping_method="rcsb_uniprot_taxon_search",
            taxon_id=taxon_id,
        )
        if not pdb_ids:
            return ConnectorResult(
                status=ConnectorStatus.NO_RESULTS,
                artifacts=(artifact,),
                mappings=(mapping,),
                warnings=(
                    "No PDB structure matched the UniProt accession and TaxID.",
                ),
            )
        records = tuple(
            {
                "record_type": "structure_hit",
                "pdb_id": pdb_id,
                "uniprot_accession": accession,
                "taxon_id": taxon_id,
                "source_url": f"https://www.rcsb.org/structure/{pdb_id}",
            }
            for pdb_id in pdb_ids
        )
        return ConnectorResult(
            status=ConnectorStatus.OK,
            records=records,
            artifacts=(artifact,),
            mappings=(mapping,),
        )

    def fetch_structure(
        self,
        pdb_id: str,
        *,
        download_mmcif: bool = True,
    ) -> ConnectorResult:
        cleaned = _clean_identifier(pdb_id, "PDB ID")
        if not re.fullmatch(
            r"(?:[0-9A-Za-z]{4}|pdb_[0-9A-Za-z]{8})",
            cleaned,
        ):
            raise ValueError("Invalid 4-character or extended PDB ID.")
        normalized_id = cleaned.upper() if len(cleaned) == 4 else cleaned.lower()
        artifacts: list[ResponseArtifact] = []
        warnings: list[str] = []
        entry = self._request(
            "GET",
            f"{self.data_base}/entry/{normalized_id}",
        )
        artifacts.append(entry)
        failure = self._http_failure(entry)
        if failure is not None:
            return failure
        payload = _json_payload(entry)
        identifiers = payload.get("rcsb_entry_container_identifiers") or {}
        entity_ids = [str(value) for value in identifiers.get(
            "polymer_entity_ids", []
        )]
        entities: list[dict[str, Any]] = []
        stable_ids = [f"PDB:{normalized_id}"]
        for entity_id in entity_ids:
            entity_artifact = self._request(
                "GET",
                (
                    f"{self.data_base}/polymer_entity/"
                    f"{normalized_id}/{entity_id}"
                ),
            )
            artifacts.append(entity_artifact)
            if entity_artifact.provenance.http_status >= 400:
                warnings.append(
                    f"Polymer entity {entity_id} metadata was unavailable."
                )
                continue
            entity_payload = _json_payload(entity_artifact)
            container_ids = entity_payload.get(
                "rcsb_polymer_entity_container_identifiers"
            ) or {}
            references = container_ids.get(
                "reference_sequence_identifiers"
            ) or []
            uniprot_ids = [
                str(item.get("database_accession"))
                for item in references
                if str(item.get("database_name", "")).casefold()
                == "uniprot"
                and item.get("database_accession")
            ]
            source_organisms = entity_payload.get(
                "rcsb_entity_source_organism"
            ) or []
            taxon_ids = [
                int(item["ncbi_taxonomy_id"])
                for item in source_organisms
                if item.get("ncbi_taxonomy_id") is not None
            ]
            stable_ids.extend(f"UniProtKB:{value}" for value in uniprot_ids)
            stable_ids.extend(f"NCBITaxon:{value}" for value in taxon_ids)
            entities.append(
                {
                    "entity_id": entity_id,
                    "label_asym_ids": container_ids.get("asym_ids") or [],
                    "auth_asym_ids": container_ids.get("auth_asym_ids") or [],
                    "uniprot_accessions": uniprot_ids,
                    "taxon_ids": taxon_ids,
                    "polymer_type": (
                        entity_payload.get("entity_poly", {}).get("type")
                    ),
                    "description": (
                        entity_payload.get("rcsb_polymer_entity", {}).get(
                            "pdbx_description"
                        )
                    ),
                }
            )

        coordinate_sha256 = None
        if download_mmcif:
            coordinate_artifact = self._request(
                "GET",
                f"{self.files_base}/{normalized_id}.cif",
            )
            artifacts.append(coordinate_artifact)
            if coordinate_artifact.provenance.http_status < 400:
                coordinate_sha256 = (
                    coordinate_artifact.provenance.raw_response_sha256
                )
            else:
                warnings.append("PDBx/mmCIF coordinates were unavailable.")

        accession_info = payload.get("rcsb_accession_info") or {}
        revision_date = accession_info.get("revision_date")
        artifacts = [
            _with_provenance(
                item,
                source_version=(
                    str(revision_date) if revision_date else None
                ),
                source_release_date=accession_info.get(
                    "initial_release_date"
                ),
                stable_ids=stable_ids,
            )
            for item in artifacts
        ]
        record = {
            "record_type": "structure",
            "pdb_id": normalized_id,
            "title": payload.get("struct", {}).get("title"),
            "experimental_methods": [
                item.get("method")
                for item in payload.get("exptl") or []
                if item.get("method")
            ],
            "resolution_angstrom": (
                payload.get("rcsb_entry_info", {}).get(
                    "resolution_combined"
                )
                or []
            ),
            "initial_release_date": accession_info.get(
                "initial_release_date"
            ),
            "revision_date": revision_date,
            "assembly_ids": identifiers.get("assembly_ids") or [],
            "entities": entities,
            "coordinate_mmcif_sha256": coordinate_sha256,
            "structure_doi": (
                f"https://doi.org/10.2210/{normalized_id.lower()}/pdb"
            ),
            "source_url": (
                f"https://www.rcsb.org/structure/{normalized_id}"
            ),
        }
        return ConnectorResult(
            status=(
                ConnectorStatus.DEGRADED
                if warnings
                else ConnectorStatus.OK
            ),
            records=(record,),
            artifacts=tuple(artifacts),
            warnings=tuple(warnings),
        )


class STRINGConnector(BaseConnector):
    source_name = "STRING"
    base_url = "https://string-db.org"
    license_url = "https://string-db.org/cgi/access"
    citation_url = "https://string-db.org/cgi/about"
    default_min_interval_seconds = 1.0

    def __init__(
        self,
        *,
        caller_identity: str = "VetEvidence-Agent",
        **kwargs: Any,
    ) -> None:
        self.caller_identity = _clean_identifier(
            caller_identity,
            "STRING caller_identity",
        )
        super().__init__(**kwargs)

    def fetch_network(
        self,
        identifiers: Sequence[str],
        *,
        taxon_id: int,
        consent_external_submission: bool,
        required_score: int = 400,
        network_type: Literal["functional", "physical"] = "functional",
    ) -> ConnectorResult:
        cleaned = tuple(
            dict.fromkeys(
                _clean_identifier(value, "STRING identifier")
                for value in identifiers
            )
        )
        if not cleaned:
            raise ValueError("At least one protein identifier is required.")
        if taxon_id < 1:
            raise ValueError("taxon_id must be a positive NCBI TaxID.")
        if not 0 <= required_score <= 1000:
            raise ValueError("required_score must be between 0 and 1000.")
        offline_content = canonical_json(
            {
                "caller_identity": self.caller_identity,
                "identifiers": cleaned,
                "network_type": network_type,
                "required_score": required_score,
                "schema_version": "vetevidence-string-request-v1",
                "taxon_id": taxon_id,
            }
        )
        offline_request = OfflineRequest(
            media_type="application/vnd.vetevidence.string-request+json",
            content=offline_content,
            sha256=sha256_bytes(offline_content.encode("utf-8")),
        )
        if not consent_external_submission:
            return ConnectorResult(
                status=ConnectorStatus.OFFLINE_EXPORT,
                acquisition_mode=AcquisitionMode.OFFLINE_REQUEST,
                offline_request=offline_request,
                warnings=(
                    "STRING submission was not sent because explicit external "
                    "data-transfer consent was not granted.",
                ),
            )
        artifacts: list[ResponseArtifact] = []

        version_artifact = self._request(
            "GET",
            f"{self.base_url}/api/json/version",
            params={"caller_identity": self.caller_identity},
        )
        artifacts.append(version_artifact)
        failure = self._http_failure(version_artifact)
        if failure is not None:
            return failure
        version_payload = _json_payload(version_artifact)
        version_row = (
            version_payload[0]
            if isinstance(version_payload, list) and version_payload
            else version_payload
        )
        if not isinstance(version_row, Mapping):
            raise ConnectorError("STRING returned an invalid version response.")
        string_version = str(
            version_row.get("string_version") or "unknown"
        )
        stable_base = str(
            version_row.get("stable_address")
            or version_row.get("string_stable_address")
            or self.base_url
        ).rstrip("/")
        if stable_base.startswith("http://"):
            stable_base = "https://" + stable_base.removeprefix("http://")
        parsed_stable_base = urlparse(stable_base)
        stable_hostname = (parsed_stable_base.hostname or "").casefold()
        if (
            parsed_stable_base.scheme != "https"
            or not (
                stable_hostname == "string-db.org"
                or stable_hostname.endswith(".string-db.org")
            )
        ):
            raise ConnectorError(
                "STRING returned an invalid version-pinned service address."
            )

        mapping_artifact = self._request(
            "POST",
            f"{stable_base}/api/json/get_string_ids",
            data={
                "identifiers": "\r".join(cleaned),
                "species": taxon_id,
                "echo_query": 1,
                "caller_identity": self.caller_identity,
            },
        )
        artifacts.append(mapping_artifact)
        failure = self._http_failure(mapping_artifact)
        if failure is not None:
            return failure.model_copy(update={"artifacts": tuple(artifacts)})
        mapping_payload = _json_payload(mapping_artifact)
        if not isinstance(mapping_payload, list):
            raise ConnectorError("STRING returned invalid identifier mappings.")
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in mapping_payload:
            if not isinstance(row, Mapping):
                continue
            query_item = row.get("queryItem")
            if query_item is None and row.get("queryIndex") is not None:
                try:
                    query_item = cleaned[int(row["queryIndex"])]
                except (IndexError, TypeError, ValueError):
                    query_item = None
            if query_item is not None:
                grouped[str(query_item)].append(dict(row))

        mappings: list[IdentifierMapping] = []
        mapped_ids: list[str] = []
        warnings: list[str] = []
        has_ambiguity = False
        for input_identifier in cleaned:
            rows = grouped.get(input_identifier, [])
            candidates = tuple(
                IdentifierCandidate(
                    identifier=str(row.get("stringId")),
                    label=(
                        str(row.get("preferredName"))
                        if row.get("preferredName")
                        else None
                    ),
                    taxon_id=(
                        int(row["ncbiTaxonId"])
                        if row.get("ncbiTaxonId") is not None
                        else taxon_id
                    ),
                )
                for row in rows
                if row.get("stringId")
            )
            unique_candidates = tuple(
                {
                    candidate.identifier: candidate
                    for candidate in candidates
                }.values()
            )
            ambiguous = len(unique_candidates) > 1
            has_ambiguity = has_ambiguity or ambiguous
            canonical = (
                unique_candidates[0].identifier
                if len(unique_candidates) == 1
                else None
            )
            if canonical:
                mapped_ids.append(canonical)
            warning = None
            if not unique_candidates:
                warning = "STRING could not map this identifier."
                warnings.append(f"Unmapped STRING identifier: {input_identifier}")
            elif ambiguous:
                warning = "STRING returned multiple candidate mappings."
                warnings.append(
                    f"Ambiguous STRING identifier: {input_identifier}"
                )
            mappings.append(
                IdentifierMapping(
                    input_identifier=input_identifier,
                    namespace="protein_identifier",
                    canonical_identifier=canonical,
                    candidates=unique_candidates,
                    ambiguous=ambiguous,
                    mapping_method="string_best_match_with_taxon",
                    taxon_id=taxon_id,
                    warning=warning,
                )
            )

        if has_ambiguity:
            artifacts = [
                _with_provenance(
                    item,
                    source_version=string_version,
                    stable_ids=(f"NCBITaxon:{taxon_id}",),
                )
                for item in artifacts
            ]
            return ConnectorResult(
                status=ConnectorStatus.DEGRADED,
                artifacts=tuple(artifacts),
                mappings=tuple(mappings),
                warnings=tuple(warnings),
            )
        if not mapped_ids:
            artifacts = [
                _with_provenance(
                    item,
                    source_version=string_version,
                    stable_ids=(f"NCBITaxon:{taxon_id}",),
                )
                for item in artifacts
            ]
            return ConnectorResult(
                status=ConnectorStatus.NO_RESULTS,
                artifacts=tuple(artifacts),
                mappings=tuple(mappings),
                warnings=tuple(warnings)
                + ("No input identifier could be mapped by STRING.",),
            )

        network_artifact = self._request(
            "POST",
            f"{stable_base}/api/json/network",
            data={
                "identifiers": "\r".join(mapped_ids),
                "species": taxon_id,
                "required_score": required_score,
                "network_type": network_type,
                "add_nodes": 0,
                "caller_identity": self.caller_identity,
            },
        )
        artifacts.append(network_artifact)
        failure = self._http_failure(network_artifact)
        if failure is not None:
            return failure.model_copy(
                update={
                    "artifacts": tuple(artifacts),
                    "mappings": tuple(mappings),
                    "warnings": tuple(warnings) + failure.warnings,
                }
            )
        network_payload = _json_payload(network_artifact)
        if not isinstance(network_payload, list):
            raise ConnectorError("STRING returned invalid network data.")
        records: list[dict[str, Any]] = []
        score_fields = {
            "nscore": "neighborhood_score",
            "fscore": "fusion_score",
            "pscore": "phylogenetic_profile_score",
            "ascore": "coexpression_score",
            "escore": "experimental_score",
            "dscore": "database_score",
            "tscore": "text_mining_score",
        }
        for row in network_payload:
            if not isinstance(row, Mapping):
                continue
            normalized = {
                "record_type": "string_interaction",
                "string_id_a": row.get("stringId_A"),
                "string_id_b": row.get("stringId_B"),
                "preferred_name_a": row.get("preferredName_A"),
                "preferred_name_b": row.get("preferredName_B"),
                "taxon_id": int(row.get("ncbiTaxonId") or taxon_id),
                "combined_score": _float_or_none(row.get("score")),
                "combined_score_role": "ranking_only",
                "network_type": network_type,
                "required_score": required_score,
                "string_version": string_version,
                "source_url": (
                    f"{stable_base}/cgi/network?"
                    f"identifiers={quote('%0d'.join(mapped_ids), safe='%')}"
                ),
            }
            for source_field, target_field in score_fields.items():
                normalized[target_field] = _float_or_none(
                    row.get(source_field)
                )
            records.append(normalized)
        stable_ids = [
            f"NCBITaxon:{taxon_id}",
            *(f"STRING:{value}" for value in mapped_ids),
        ]
        artifacts = [
            _with_provenance(
                item,
                source_version=string_version,
                stable_ids=stable_ids,
            )
            for item in artifacts
        ]
        if not records:
            return ConnectorResult(
                status=ConnectorStatus.NO_RESULTS,
                artifacts=tuple(artifacts),
                mappings=tuple(mappings),
                warnings=tuple(warnings)
                + (
                    "STRING mapped the proteins but returned no interaction "
                    "at the selected score threshold.",
                ),
            )
        return ConnectorResult(
            status=(
                ConnectorStatus.DEGRADED
                if warnings
                else ConnectorStatus.OK
            ),
            records=tuple(records),
            artifacts=tuple(artifacts),
            mappings=tuple(mappings),
            warnings=tuple(warnings),
        )


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fraction_or_none(value: Any) -> float | None:
    number = _float_or_none(value)
    if number is None:
        return None
    if not 0 <= number <= 1:
        raise ConnectorError(
            "DAVID returned an invalid mapping fraction outside [0, 1]."
        )
    return number


DEFAULT_DAVID_CATEGORIES = (
    "GOTERM_BP_DIRECT",
    "GOTERM_CC_DIRECT",
    "GOTERM_MF_DIRECT",
    "KEGG_PATHWAY",
    "REACTOME_PATHWAY",
)

DAVID_TAXON_SCIENTIFIC_NAMES = {
    9031: "Gallus gallus",
    9606: "Homo sapiens",
    9615: "Canis lupus familiaris",
    9685: "Felis catus",
    9823: "Sus scrofa",
    9913: "Bos taurus",
    10090: "Mus musculus",
    10116: "Rattus norvegicus",
}


class DAVIDConnector(BaseConnector):
    source_name = "DAVID"
    soap_url = (
        "https://davidbioinformatics.nih.gov/webservice/services/"
        "DAVIDWebService.DAVIDWebServiceHttpSoap11Endpoint/"
    )
    license_url = "https://davidbioinformatics.nih.gov/"
    citation_url = "https://davidbioinformatics.nih.gov/helps/FAQs.html"
    default_min_interval_seconds = 1.0

    def __init__(
        self,
        *,
        registered_email: str | None = None,
        knowledgebase_version: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.registered_email = (
            registered_email.strip() if registered_email else None
        )
        self.knowledgebase_version = (
            knowledgebase_version.strip()
            if knowledgebase_version
            else None
        )
        super().__init__(**kwargs)

    def export_enrichment_request(
        self,
        identifiers: Sequence[str],
        *,
        taxon_id: int,
        background: Sequence[str],
        id_type: str = "ENTREZ_GENE_ID",
        categories: Sequence[str] = DEFAULT_DAVID_CATEGORIES,
        max_ease_p_value: float = 1.0,
        min_count: int = 2,
    ) -> OfflineRequest:
        manifest = _david_manifest(
            identifiers,
            taxon_id=taxon_id,
            background=background,
            id_type=id_type,
            categories=categories,
            max_ease_p_value=max_ease_p_value,
            min_count=min_count,
        )
        content = canonical_json(manifest)
        return OfflineRequest(
            media_type="application/vnd.vetevidence.david-request+json",
            content=content,
            sha256=sha256_bytes(content.encode("utf-8")),
        )

    def enrich(
        self,
        identifiers: Sequence[str],
        *,
        taxon_id: int,
        background: Sequence[str],
        consent_external_submission: bool,
        id_type: str = "ENTREZ_GENE_ID",
        categories: Sequence[str] = DEFAULT_DAVID_CATEGORIES,
        max_ease_p_value: float = 1.0,
        min_count: int = 2,
    ) -> ConnectorResult:
        manifest = _david_manifest(
            identifiers,
            taxon_id=taxon_id,
            background=background,
            id_type=id_type,
            categories=categories,
            max_ease_p_value=max_ease_p_value,
            min_count=min_count,
        )
        offline = self.export_enrichment_request(
            manifest["identifiers"],
            taxon_id=taxon_id,
            background=manifest["background"],
            id_type=id_type,
            categories=manifest["categories"],
            max_ease_p_value=max_ease_p_value,
            min_count=min_count,
        )
        if not consent_external_submission:
            return ConnectorResult(
                status=ConnectorStatus.OFFLINE_EXPORT,
                acquisition_mode=AcquisitionMode.OFFLINE_REQUEST,
                offline_request=offline,
                warnings=(
                    "DAVID submission was not sent because explicit external "
                    "data-transfer consent was not granted.",
                ),
            )
        if not self.registered_email:
            return ConnectorResult(
                status=ConnectorStatus.DEGRADED,
                acquisition_mode=AcquisitionMode.OFFLINE_REQUEST,
                offline_request=offline,
                warnings=(
                    "DAVID Web Service requires a registered organization "
                    "email; an offline request was exported instead.",
                ),
            )

        artifacts: list[ResponseArtifact] = []
        warnings: list[str] = []
        auth = self._soap_call(
            "authenticate",
            (self.registered_email,),
        )
        artifacts.append(auth)
        failure = self._http_failure(auth)
        if failure is not None:
            return failure.model_copy(
                update={
                    "offline_request": offline,
                    "warnings": failure.warnings
                    + ("DAVID authentication could not be completed.",),
                }
            )
        if _soap_return_text(auth).casefold() not in {"true", "1"}:
            return ConnectorResult(
                status=ConnectorStatus.DEGRADED,
                artifacts=tuple(artifacts),
                offline_request=offline,
                warnings=(
                    "DAVID rejected the registered email; no data was submitted.",
                ),
            )

        target_add = self._soap_call(
            "addList",
            (
                ",".join(manifest["identifiers"]),
                manifest["id_type"],
                "VetEvidence targets",
                0,
            ),
        )
        artifacts.append(target_add)
        if target_add.provenance.http_status >= 400:
            return ConnectorResult(
                status=ConnectorStatus.DEGRADED,
                artifacts=tuple(artifacts),
                offline_request=offline,
                warnings=("DAVID target-list upload failed.",),
            )
        target_mapping_fraction = _fraction_or_none(
            _soap_return_text(target_add)
        )

        species_options = self._soap_call("getSpecies", ())
        artifacts.append(species_options)
        failure = self._http_failure(species_options)
        if failure is not None:
            return failure.model_copy(
                update={
                    "artifacts": tuple(artifacts),
                    "offline_request": offline,
                    "warnings": failure.warnings
                    + ("DAVID species options could not be retrieved.",),
                }
            )
        species_index = _david_species_index(
            _soap_return_text(species_options),
            taxon_id,
        )
        if species_index is None:
            return ConnectorResult(
                status=ConnectorStatus.DEGRADED,
                artifacts=tuple(artifacts),
                offline_request=offline,
                warnings=(
                    "DAVID could not reliably map the requested TaxID to one "
                    "of the uploaded gene list's species options; enrichment "
                    "was not run.",
                ),
            )
        species = self._soap_call(
            "setCurrentSpecies",
            (str(species_index),),
        )
        artifacts.append(species)
        failure = self._http_failure(species)
        if failure is not None:
            return failure.model_copy(
                update={
                    "artifacts": tuple(artifacts),
                    "offline_request": offline,
                    "warnings": failure.warnings
                    + ("DAVID species selection failed.",),
                }
            )
        current_species = self._soap_call("getCurrentSpecies", ())
        artifacts.append(current_species)
        failure = self._http_failure(current_species)
        if failure is not None:
            return failure.model_copy(
                update={
                    "artifacts": tuple(artifacts),
                    "offline_request": offline,
                    "warnings": failure.warnings
                    + ("DAVID species selection could not be verified.",),
                }
            )
        if species_index not in _david_species_indices(
            _soap_return_text(current_species)
        ):
            return ConnectorResult(
                status=ConnectorStatus.DEGRADED,
                artifacts=tuple(artifacts),
                offline_request=offline,
                warnings=(
                    "DAVID did not confirm the selected species index; "
                    "enrichment was not run.",
                ),
            )

        background_add = self._soap_call(
            "addList",
            (
                ",".join(manifest["background"]),
                manifest["id_type"],
                "VetEvidence background",
                1,
            ),
        )
        artifacts.append(background_add)
        if background_add.provenance.http_status >= 400:
            return ConnectorResult(
                status=ConnectorStatus.DEGRADED,
                artifacts=tuple(artifacts),
                offline_request=offline,
                warnings=("DAVID background-list upload failed.",),
            )
        background_mapping_fraction = _fraction_or_none(
            _soap_return_text(background_add)
        )

        category_artifact = self._soap_call(
            "setCategories",
            (",".join(manifest["categories"]),),
        )
        artifacts.append(category_artifact)
        failure = self._http_failure(category_artifact)
        if failure is not None:
            return failure.model_copy(
                update={
                    "artifacts": tuple(artifacts),
                    "offline_request": offline,
                    "warnings": failure.warnings
                    + ("DAVID category selection failed.",),
                }
            )
        confirmed_categories = {
            item.strip()
            for item in _soap_return_text(category_artifact).split(",")
            if item.strip()
        }
        requested_categories = set(manifest["categories"])
        if confirmed_categories != requested_categories:
            return ConnectorResult(
                status=ConnectorStatus.DEGRADED,
                artifacts=tuple(artifacts),
                offline_request=offline,
                warnings=(
                    "DAVID did not confirm exactly the requested annotation "
                    "categories; enrichment was not run.",
                ),
            )

        chart = self._soap_call(
            "getChartReport",
            (max_ease_p_value, min_count),
        )
        artifacts.append(chart)
        failure = self._http_failure(chart)
        if failure is not None:
            return failure.model_copy(
                update={
                    "artifacts": tuple(artifacts),
                    "offline_request": offline,
                    "warnings": tuple(warnings) + failure.warnings,
                }
            )
        records = _parse_david_chart(chart.raw_response)
        version = "not_reported_by_soap"
        stable_ids = [
            f"NCBITaxon:{taxon_id}",
            *(f"{id_type}:{value}" for value in manifest["identifiers"]),
        ]
        artifacts = [
            _with_provenance(
                artifact,
                stable_ids=stable_ids,
            )
            for artifact in artifacts
        ]
        if self.knowledgebase_version is None:
            warnings.append(
                "DAVID SOAP does not report its knowledgebase release; "
                "no verified source version was recorded."
            )
        else:
            warnings.append(
                "The configured DAVID knowledgebase version is user-asserted "
                "and was not recorded as verified upstream provenance."
            )
        for record in records:
            record.update(
                {
                    "taxon_id": taxon_id,
                    "target_mapping_fraction": target_mapping_fraction,
                    "background_mapping_fraction": (
                        background_mapping_fraction
                    ),
                    "knowledgebase_version": version,
                    "user_asserted_knowledgebase_version": (
                        self.knowledgebase_version
                    ),
                    "source_url": (
                        "https://davidbioinformatics.nih.gov/chartReport.html"
                    ),
                }
            )
        if not records:
            return ConnectorResult(
                status=ConnectorStatus.NO_RESULTS,
                artifacts=tuple(artifacts),
                warnings=tuple(warnings)
                + (
                    "DAVID completed the request but returned no enriched term "
                    "at the selected thresholds.",
                ),
            )
        return ConnectorResult(
            status=(
                ConnectorStatus.DEGRADED
                if warnings
                else ConnectorStatus.OK
            ),
            records=tuple(records),
            artifacts=tuple(artifacts),
            warnings=tuple(warnings),
        )

    def _soap_call(
        self,
        operation: str,
        args: Sequence[Any],
    ) -> ResponseArtifact:
        arguments = "".join(
            f"<sam:args{index}>{escape(str(value))}</sam:args{index}>"
            for index, value in enumerate(args)
        )
        envelope = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<soapenv:Envelope '
            'xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
            'xmlns:sam="http://service.session.sample">'
            f"<soapenv:Body><sam:{operation}>{arguments}</sam:{operation}>"
            "</soapenv:Body></soapenv:Envelope>"
        )
        return self._request(
            "POST",
            self.soap_url,
            content=envelope,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f"urn:{operation}",
            },
        )


def _david_manifest(
    identifiers: Sequence[str],
    *,
    taxon_id: int,
    background: Sequence[str],
    id_type: str,
    categories: Sequence[str],
    max_ease_p_value: float,
    min_count: int,
) -> dict[str, Any]:
    targets = tuple(
        dict.fromkeys(
            _clean_identifier(value, "DAVID target identifier")
            for value in identifiers
        )
    )
    background_ids = tuple(
        dict.fromkeys(
            _clean_identifier(value, "DAVID background identifier")
            for value in background
        )
    )
    cleaned_categories = tuple(
        dict.fromkeys(
            _clean_identifier(value, "DAVID category")
            for value in categories
        )
    )
    if not targets:
        raise ValueError("DAVID requires at least one target identifier.")
    if taxon_id < 1:
        raise ValueError("DAVID requires a positive NCBI TaxID.")
    if not background_ids:
        raise ValueError("DAVID requires an explicit background gene set.")
    if not set(targets).issubset(background_ids):
        raise ValueError(
            "Every DAVID target must be present in the explicit background."
        )
    if len(targets) > 3000 or len(background_ids) > 3000:
        raise ValueError("DAVID v0.4 connector is limited to 3000 identifiers.")
    if not cleaned_categories:
        raise ValueError("At least one DAVID annotation category is required.")
    if not 0 < max_ease_p_value <= 1:
        raise ValueError("max_ease_p_value must be in (0, 1].")
    if min_count < 1:
        raise ValueError("min_count must be positive.")
    return {
        "schema_version": "vetevidence-david-request-v1",
        "identifiers": targets,
        "taxon_id": taxon_id,
        "background": background_ids,
        "id_type": _clean_identifier(id_type, "DAVID id_type"),
        "categories": cleaned_categories,
        "max_ease_p_value": max_ease_p_value,
        "min_count": min_count,
    }


def _david_species_index(species_text: str, taxon_id: int) -> int | None:
    expected_name = DAVID_TAXON_SCIENTIFIC_NAMES.get(taxon_id)
    if expected_name is None:
        return None
    options = [
        re.sub(r"\s*\(\d+\)\s*$", "", item).strip()
        for item in species_text.split(",")
        if item.strip()
    ]
    matches = [
        index
        for index, name in enumerate(options)
        if name.casefold() == expected_name.casefold()
    ]
    return matches[0] if len(matches) == 1 else None


def _david_species_indices(value: str) -> set[int]:
    indices: set[int] = set()
    for item in value.split(","):
        try:
            index = int(item.strip())
        except ValueError:
            continue
        if index >= 0:
            indices.add(index)
    return indices


def _soap_return_text(artifact: ResponseArtifact) -> str:
    try:
        root = ET.fromstring(artifact.raw_response)
    except ET.ParseError as exc:
        raise ConnectorError("DAVID returned invalid SOAP XML.") from exc
    for node in root.iter():
        if _local_name(node.tag) == "return":
            return (node.text or "").strip()
    return ""


def _parse_david_chart(payload: bytes) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ConnectorError("DAVID returned invalid chart-report XML.") from exc
    records: list[dict[str, Any]] = []
    numeric_float = {
        "EASEBonferroni",
        "afdr",
        "benjamini",
        "bonferroni",
        "ease",
        "fisher",
        "foldEnrichment",
        "percent",
        "rfdr",
    }
    numeric_int = {
        "id",
        "listHits",
        "listTotals",
        "popHits",
        "popTotals",
    }
    for return_node in (
        node for node in root.iter() if _local_name(node.tag) == "return"
    ):
        row: dict[str, Any] = {}
        for child in return_node:
            key = _local_name(child.tag)
            value = (child.text or "").strip()
            if key in numeric_float:
                row[key] = _float_or_none(value)
            elif key in numeric_int:
                try:
                    row[key] = int(value)
                except ValueError:
                    row[key] = None
            else:
                row[key] = value or None
        if not row:
            continue
        raw_term_name = str(row.get("termName") or "").strip()
        term_id, term_name = _split_david_term_name(
            raw_term_name,
            record_id=row.get("id"),
        )
        records.append(
            {
                "record_type": "david_enrichment",
                "term_id": term_id,
                "term_name": term_name,
                "david_record_id": row.get("id"),
                "category": row.get("categoryName"),
                "gene_ids": [
                    item.strip()
                    for item in str(row.get("geneIds") or "").split(",")
                    if item.strip()
                ],
                "hit_count": row.get("listHits"),
                "list_total": row.get("listTotals"),
                "background_hit_count": row.get("popHits"),
                "background_total": row.get("popTotals"),
                "p_value": row.get("ease"),
                "fisher_p_value": row.get("fisher"),
                "bh_adjusted_p_value": row.get("benjamini"),
                "bonferroni_adjusted_p_value": row.get("bonferroni"),
                "fold_enrichment": row.get("foldEnrichment"),
                "adjustment_method": "benjamini_hochberg",
            }
        )
    return records


def _split_david_term_name(
    value: str,
    *,
    record_id: Any,
) -> tuple[str, str]:
    identifier, separator, label = value.partition("~")
    if separator and identifier.strip():
        cleaned_identifier = identifier.strip()
        return cleaned_identifier, label.strip() or cleaned_identifier
    if value:
        return value, value
    fallback = f"DAVID_RECORD:{record_id}" if record_id is not None else "unreported"
    return fallback, fallback


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def export_connector_result(result: ConnectorResult) -> OfflineRequest:
    """Create a deterministic JSON bundle suitable for a download button."""

    payload = result.model_dump(mode="json")
    records = payload.get("records", [])
    payload["export_metadata"] = {
        "schema_version": CONNECTOR_EXPORT_SCHEMA_VERSION,
        "parser_version": CONNECTOR_PARSER_VERSION,
        "records_sha256": sha256_bytes(
            canonical_json(records).encode("utf-8")
        ),
        "record_sha256": [
            sha256_bytes(canonical_json(record).encode("utf-8"))
            for record in records
        ],
        "raw_response_storage": "external_connector_artifacts",
    }
    content = canonical_json(payload)
    return OfflineRequest(
        media_type="application/vnd.vetevidence.connector-result+json",
        content=content,
        sha256=sha256_bytes(content.encode("utf-8")),
    )


class DatabaseConnectorHub:
    """Thin facade intended for UI orchestration without hiding source clients."""

    def __init__(
        self,
        *,
        pubchem: PubChemConnector | None = None,
        uniprot: UniProtConnector | None = None,
        ncbi: NCBIConnector | None = None,
        rcsb: RCSBConnector | None = None,
        string: STRINGConnector | None = None,
        david: DAVIDConnector | None = None,
    ) -> None:
        self.pubchem = pubchem or PubChemConnector()
        self.uniprot = uniprot or UniProtConnector()
        self.ncbi = ncbi or NCBIConnector()
        self.rcsb = rcsb or RCSBConnector()
        self.string = string or STRINGConnector()
        self.david = david or DAVIDConnector()
        self._connectors = (
            self.pubchem,
            self.uniprot,
            self.ncbi,
            self.rcsb,
            self.string,
            self.david,
        )

    def close(self) -> None:
        for connector in self._connectors:
            connector.close()

    def __enter__(self) -> DatabaseConnectorHub:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def export(result: ConnectorResult) -> OfflineRequest:
        return export_connector_result(result)


__all__ = [
    "AcquisitionMode",
    "BaseConnector",
    "ConnectorError",
    "ConnectorResult",
    "ConnectorStatus",
    "ConnectorTransportError",
    "DatabaseConnectorHub",
    "DAVIDConnector",
    "DatabaseEvidenceClass",
    "DEFAULT_DAVID_CATEGORIES",
    "IdentifierCandidate",
    "IdentifierMapping",
    "NCBIConnector",
    "OfflineRequest",
    "ProvenanceRecord",
    "PubChemConnector",
    "RCSBConnector",
    "RequestExecutor",
    "ResponseArtifact",
    "STRINGConnector",
    "UniProtConnector",
    "canonical_json",
    "export_connector_result",
    "normalized_request",
    "sha256_bytes",
]
