from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from vetevidence.database_connectors import (
    AcquisitionMode,
    BaseConnector,
    ConnectorError,
    ConnectorResult,
    ConnectorStatus,
    ConnectorTransportError,
    DatabaseEvidenceClass,
    IdentifierCandidate,
    IdentifierMapping,
    OfflineRequest,
    ResponseArtifact,
    canonical_json,
    sha256_bytes,
)


_OMIM_MIM_PATTERN = re.compile(
    r"^(?:(?:OMIM|MIM)\s*:\s*)?(\d{6})$",
    flags=re.IGNORECASE,
)
_DRUGBANK_ID_PATTERN = re.compile(r"^DB\d{5,7}$", flags=re.IGNORECASE)
_DRUGBANK_BIO_ENTITY_ID_PATTERN = re.compile(
    r"^BE\d+$",
    flags=re.IGNORECASE,
)


def _clean_query(value: str, label: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{label} cannot be blank.")
    return cleaned


def _offline_result(
    *,
    media_type: str,
    payload: Mapping[str, Any],
    warning: str,
) -> ConnectorResult:
    content = canonical_json(payload)
    return ConnectorResult(
        status=ConnectorStatus.OFFLINE_EXPORT,
        acquisition_mode=AcquisitionMode.OFFLINE_REQUEST,
        evidence_class=DatabaseEvidenceClass.CURATED_DATABASE,
        offline_request=OfflineRequest(
            media_type=media_type,
            content=content,
            sha256=sha256_bytes(content.encode("utf-8")),
        ),
        warnings=(warning,),
    )


def _online_result(
    status: ConnectorStatus,
    **updates: Any,
) -> ConnectorResult:
    return ConnectorResult(
        status=status,
        acquisition_mode=AcquisitionMode.ONLINE_API,
        evidence_class=DatabaseEvidenceClass.CURATED_DATABASE,
        **updates,
    )


def _payload_json(artifact: ResponseArtifact) -> Any:
    try:
        return json.loads(artifact.raw_response)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ConnectorError(
            f"{artifact.provenance.source_name} returned invalid JSON."
        ) from exc


def _update_provenance(
    artifact: ResponseArtifact,
    *,
    stable_ids: Sequence[str] = (),
    source_version: str | None = None,
) -> ResponseArtifact:
    updates: dict[str, Any] = {
        "stable_ids": tuple(dict.fromkeys(stable_ids)),
    }
    if source_version:
        updates["source_version"] = source_version
    provenance = artifact.provenance.model_copy(update=updates)
    return artifact.model_copy(update={"provenance": provenance})


def _mapping_list(value: Any, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ConnectorError(f"{label} must be a JSON list.")
    if not all(isinstance(item, Mapping) for item in value):
        raise ConnectorError(f"{label} contains a non-object item.")
    return list(value)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,;]", value) if item.strip()]
    if isinstance(value, (list, tuple)):
        return [
            str(item).strip()
            for item in value
            if item is not None and str(item).strip()
        ]
    return []


class OMIMConnector(BaseConnector):
    """Credential-gated access to the official OMIM API."""

    source_name = "OMIM"
    base_url = "https://api.omim.org/api"
    license_url = "https://omim.org/help/agreement"
    citation_url = "https://omim.org/help/faq"
    default_min_interval_seconds = 0.2

    def __init__(
        self,
        *,
        api_key: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.api_key = api_key.strip() if api_key else None
        super().__init__(**kwargs)

    def fetch(
        self,
        query: str,
        *,
        max_results: int = 10,
    ) -> ConnectorResult:
        cleaned = _clean_query(query, "OMIM query")
        match = _OMIM_MIM_PATTERN.fullmatch(cleaned)
        if match:
            return self.fetch_entry(match.group(1))
        return self.search_entries(cleaned, max_results=max_results)

    def fetch_entry(self, mim_number: str | int) -> ConnectorResult:
        cleaned = _clean_query(str(mim_number), "OMIM MIM number")
        match = _OMIM_MIM_PATTERN.fullmatch(cleaned)
        if not match:
            raise ValueError("OMIM MIM number must contain exactly six digits.")
        canonical_mim = match.group(1)
        missing = self._missing_key_result(
            operation="fetch_entry",
            parameters={"mim_number": canonical_mim},
        )
        if missing is not None:
            return missing

        endpoint = f"{self.base_url}/entry"
        try:
            artifact = self._request(
                "GET",
                endpoint,
                params={
                    "mimNumber": canonical_mim,
                    "include": "geneMap",
                    "format": "json",
                    "apiKey": self.api_key,
                },
            )
        except ConnectorTransportError:
            return _online_result(
                ConnectorStatus.DEGRADED,
                warnings=(
                    "OMIM transport failed; no entry was inferred or cached.",
                ),
            )
        failure = self._http_failure(artifact)
        if failure is not None:
            return failure
        return self._parse_response(
            artifact,
            expected_mim_number=canonical_mim,
            search_response=False,
        )

    def search_entries(
        self,
        query: str,
        *,
        max_results: int = 10,
    ) -> ConnectorResult:
        cleaned = _clean_query(query, "OMIM search query")
        if not 1 <= max_results <= 100:
            raise ValueError("max_results must be between 1 and 100.")
        missing = self._missing_key_result(
            operation="search_entries",
            parameters={"query": cleaned, "max_results": max_results},
        )
        if missing is not None:
            return missing

        endpoint = f"{self.base_url}/entry/search"
        try:
            artifact = self._request(
                "GET",
                endpoint,
                params={
                    "search": cleaned,
                    "include": "geneMap",
                    "format": "json",
                    "start": 0,
                    "limit": max_results,
                    "apiKey": self.api_key,
                },
            )
        except ConnectorTransportError:
            return _online_result(
                ConnectorStatus.DEGRADED,
                warnings=(
                    "OMIM transport failed; no search result was inferred.",
                ),
            )
        failure = self._http_failure(artifact)
        if failure is not None:
            return failure
        return self._parse_response(
            artifact,
            search_response=True,
            max_results=max_results,
        )

    search = search_entries

    def _missing_key_result(
        self,
        *,
        operation: str,
        parameters: Mapping[str, Any],
    ) -> ConnectorResult | None:
        if self.api_key:
            return None
        return _offline_result(
            media_type="application/vnd.vetevidence.omim-request+json",
            payload={
                "official_api_base": self.base_url,
                "operation": operation,
                "parameters": parameters,
                "required_configuration": ["OMIM_API_KEY"],
                "schema_version": "vetevidence-omim-request-v1",
            },
            warning=(
                "OMIM_API_KEY is not configured, so VetEvidence sent no "
                "request. Configure an authorized OMIM API key and retry."
            ),
        )

    def _parse_response(
        self,
        artifact: ResponseArtifact,
        *,
        search_response: bool,
        expected_mim_number: str | None = None,
        max_results: int | None = None,
    ) -> ConnectorResult:
        try:
            payload = _payload_json(artifact)
            entries, source_version = _extract_omim_entries(
                payload,
                search_response=search_response,
            )
            if max_results is not None:
                entries = entries[:max_results]
            records = tuple(_normalize_omim_entry(entry) for entry in entries)
        except ConnectorError:
            return _online_result(
                ConnectorStatus.DEGRADED,
                artifacts=(artifact,),
                warnings=(
                    "OMIM returned JSON that did not match the documented "
                    "entry response schema.",
                ),
            )

        if not records:
            return _online_result(
                ConnectorStatus.NO_RESULTS,
                artifacts=(artifact,),
                warnings=("OMIM returned no matching entries.",),
            )

        stable_ids = tuple(
            f"OMIM:{record['mim_number']}" for record in records
        )
        artifact = _update_provenance(
            artifact,
            stable_ids=stable_ids,
            source_version=source_version,
        )
        if expected_mim_number is not None and (
            len(records) != 1
            or records[0]["mim_number"] != expected_mim_number
        ):
            return _online_result(
                ConnectorStatus.DEGRADED,
                records=records,
                artifacts=(artifact,),
                warnings=(
                    "OMIM did not return exactly the requested MIM entry; "
                    "the response was retained but not treated as a match.",
                ),
            )
        return _online_result(
            ConnectorStatus.OK,
            records=records,
            artifacts=(artifact,),
        )


def _extract_omim_entries(
    payload: Any,
    *,
    search_response: bool,
) -> tuple[list[Mapping[str, Any]], str | None]:
    if not isinstance(payload, Mapping):
        raise ConnectorError("OMIM response root must be an object.")
    omim = payload.get("omim")
    if not isinstance(omim, Mapping):
        raise ConnectorError("OMIM response is missing its omim object.")

    if search_response:
        search = omim.get("searchResponse")
        if not isinstance(search, Mapping):
            raise ConnectorError("OMIM search response is missing searchResponse.")
        raw_entries = search.get("entryList")
    else:
        raw_entries = omim.get("entryList")
    wrappers = _mapping_list(raw_entries, "OMIM entryList")
    entries: list[Mapping[str, Any]] = []
    for wrapper in wrappers:
        entry = wrapper.get("entry")
        if not isinstance(entry, Mapping):
            raise ConnectorError("OMIM entryList item is missing entry.")
        entries.append(entry)
    version = omim.get("version")
    return entries, str(version) if version not in (None, "") else None


def _normalize_omim_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    raw_mim = entry.get("mimNumber")
    mim_number = str(raw_mim).strip() if raw_mim is not None else ""
    if not mim_number.isdigit():
        raise ConnectorError("OMIM entry has no numeric mimNumber.")

    titles = entry.get("titles")
    if titles is not None and not isinstance(titles, Mapping):
        raise ConnectorError("OMIM entry titles must be an object.")
    title = titles.get("preferredTitle") if isinstance(titles, Mapping) else None

    gene_map = entry.get("geneMap")
    if gene_map is not None and not isinstance(gene_map, Mapping):
        raise ConnectorError("OMIM geneMap must be an object.")
    gene_symbols = (
        _string_list(gene_map.get("geneSymbols"))
        if isinstance(gene_map, Mapping)
        else []
    )
    phenotype_values: list[Any] = []
    if isinstance(gene_map, Mapping) and "phenotypeMapList" in gene_map:
        phenotype_values.extend(
            _mapping_list(
                gene_map["phenotypeMapList"],
                "OMIM geneMap phenotypeMapList",
            )
        )
    if "phenotypeMapList" in entry:
        phenotype_values.extend(
            _mapping_list(
                entry["phenotypeMapList"],
                "OMIM entry phenotypeMapList",
            )
        )

    phenotype_mappings: list[dict[str, Any]] = []
    for wrapper in phenotype_values:
        phenotype = wrapper.get("phenotypeMap")
        if not isinstance(phenotype, Mapping):
            raise ConnectorError(
                "OMIM phenotypeMapList item is missing phenotypeMap."
            )
        phenotype_mappings.append(
            {
                "gene_mim_number": _string_or_none(
                    phenotype.get("mimNumber")
                ),
                "phenotype": _string_or_none(phenotype.get("phenotype")),
                "phenotype_mim_number": _string_or_none(
                    phenotype.get("phenotypeMimNumber")
                ),
                "phenotypic_series_number": _string_or_none(
                    phenotype.get("phenotypicSeriesNumber")
                ),
                "mapping_key": phenotype.get("phenotypeMappingKey"),
                "inheritance": _string_or_none(
                    phenotype.get("phenotypeInheritance")
                ),
            }
        )

    return {
        "record_type": "omim_entry",
        "mim_number": mim_number,
        "title": _string_or_none(title),
        "entry_prefix": _string_or_none(entry.get("prefix")),
        "status": _string_or_none(entry.get("status")),
        "gene_symbols": gene_symbols,
        "phenotype_mappings": phenotype_mappings,
        "taxon_id": 9606,
        "source_url": f"https://omim.org/entry/{mim_number}",
    }


class DrugBankConnector(BaseConnector):
    """License- and credential-gated DrugBank Discovery API connector."""

    source_name = "DrugBank"
    base_url = "https://api.drugbank.com/discovery/v1"
    license_url = (
        "https://trust.drugbank.com/drugbank-trust-center/terms-of-use"
    )
    citation_url = "https://dev.drugbank.com/publications"
    default_min_interval_seconds = 0.02

    def __init__(
        self,
        *,
        api_key: str | None = None,
        license_confirmed: bool = False,
        **kwargs: Any,
    ) -> None:
        self.api_key = api_key.strip() if api_key else None
        self.license_confirmed = license_confirmed is True
        super().__init__(**kwargs)

    def fetch_drug(
        self,
        identifier: str,
        *,
        include_bonds: bool = True,
        max_results: int = 20,
    ) -> ConnectorResult:
        cleaned = _clean_query(identifier, "DrugBank drug identifier")
        if not 1 <= max_results <= 50:
            raise ValueError("max_results must be between 1 and 50.")
        direct_id = (
            cleaned.upper()
            if _DRUGBANK_ID_PATTERN.fullmatch(cleaned)
            else None
        )
        missing = self._access_gate_result(
            identifier=cleaned,
            identifier_type="drugbank_id" if direct_id else "name",
            include_bonds=include_bonds,
            max_results=max_results,
        )
        if missing is not None:
            return missing

        artifacts: list[ResponseArtifact] = []
        mappings: list[IdentifierMapping] = []
        if direct_id is None:
            resolution = self._resolve_name(
                cleaned,
                max_results=max_results,
            )
            if isinstance(resolution, ConnectorResult):
                return resolution
            direct_id, search_artifact, mapping = resolution
            artifacts.append(search_artifact)
            mappings.append(mapping)
        else:
            mappings.append(
                IdentifierMapping(
                    input_identifier=cleaned,
                    namespace="drugbank_id",
                    canonical_identifier=direct_id,
                    mapping_method="drugbank_id_identity",
                )
            )

        detail_url = f"{self.base_url}/drugs/{quote(direct_id, safe='')}"
        try:
            detail_artifact = self._request(
                "GET",
                detail_url,
                params={"include_references": "true"},
                headers={
                    "Accept": "application/json",
                    "Authorization": self.api_key,
                },
            )
        except ConnectorTransportError:
            return _online_result(
                ConnectorStatus.DEGRADED,
                artifacts=tuple(artifacts),
                mappings=tuple(mappings),
                warnings=(
                    "DrugBank transport failed while retrieving the drug "
                    "detail; no record was inferred.",
                ),
            )
        artifacts.append(detail_artifact)
        detail_failure = self._http_failure(detail_artifact)
        if detail_failure is not None:
            return detail_failure.model_copy(
                update={
                    "artifacts": tuple(artifacts),
                    "mappings": tuple(mappings),
                }
            )
        try:
            detail_payload = _payload_json(detail_artifact)
            drug_record = _normalize_drugbank_drug(
                detail_payload,
                expected_id=direct_id,
            )
        except ConnectorError:
            return _online_result(
                ConnectorStatus.DEGRADED,
                artifacts=tuple(artifacts),
                mappings=tuple(mappings),
                warnings=(
                    "DrugBank returned JSON that did not match the "
                    "documented drug detail schema.",
                ),
            )
        detail_stable_ids = [f"DrugBank:{direct_id}"]
        if drug_record.get("inchikey"):
            detail_stable_ids.append(f"InChIKey:{drug_record['inchikey']}")
        detail_artifact = _update_provenance(
            detail_artifact,
            stable_ids=detail_stable_ids,
        )
        artifacts[-1] = detail_artifact

        records: list[dict[str, Any]] = [drug_record]
        warnings: list[str] = []
        status = ConnectorStatus.OK
        if include_bonds:
            bond_status, bond_records, bond_artifacts, bond_warnings = (
                self._fetch_bonds(direct_id, max_results=max_results)
            )
            artifacts.extend(bond_artifacts)
            records.extend(bond_records)
            warnings.extend(bond_warnings)
            if bond_status == ConnectorStatus.DEGRADED:
                status = ConnectorStatus.DEGRADED

        return _online_result(
            status,
            records=tuple(records),
            artifacts=tuple(artifacts),
            mappings=tuple(mappings),
            warnings=tuple(warnings),
        )

    fetch = fetch_drug

    def _access_gate_result(
        self,
        *,
        identifier: str,
        identifier_type: str,
        include_bonds: bool,
        max_results: int,
    ) -> ConnectorResult | None:
        required: list[str] = []
        if not self.api_key:
            required.append("DRUGBANK_API_KEY")
        if not self.license_confirmed:
            required.append("DRUGBANK_LICENSE_CONFIRMATION")
        if not required:
            return None
        return _offline_result(
            media_type="application/vnd.vetevidence.drugbank-request+json",
            payload={
                "official_api_base": self.base_url,
                "operation": "fetch_drug",
                "parameters": {
                    "identifier": identifier,
                    "identifier_type": identifier_type,
                    "include_bonds": include_bonds,
                    "max_results": max_results,
                },
                "required_configuration": required,
                "schema_version": "vetevidence-drugbank-request-v1",
            },
            warning=(
                "DrugBank access requires both an authorized API key and "
                "confirmation that the active license permits this query. "
                "VetEvidence sent no request."
            ),
        )

    def _resolve_name(
        self,
        name: str,
        *,
        max_results: int,
    ) -> tuple[str, ResponseArtifact, IdentifierMapping] | ConnectorResult:
        endpoint = f"{self.base_url}/drugs"
        escaped_name = name.replace("\\", "\\\\").replace('"', '\\"')
        try:
            artifact = self._request(
                "GET",
                endpoint,
                params={
                    "q": f'name:"{escaped_name}"',
                    "per_page": min(max_results + 1, 50),
                },
                headers={
                    "Accept": "application/json",
                    "Authorization": self.api_key,
                },
            )
        except ConnectorTransportError:
            return _online_result(
                ConnectorStatus.DEGRADED,
                warnings=(
                    "DrugBank transport failed during name resolution; no "
                    "drug was selected.",
                ),
            )
        failure = self._http_failure(artifact)
        if failure is not None:
            return failure
        try:
            payload = _payload_json(artifact)
            candidates = _extract_drugbank_search_candidates(payload)
        except ConnectorError:
            return _online_result(
                ConnectorStatus.DEGRADED,
                artifacts=(artifact,),
                warnings=(
                    "DrugBank returned JSON that did not match the "
                    "documented drug search schema.",
                ),
            )
        artifact = _update_provenance(
            artifact,
            stable_ids=[
                f"DrugBank:{str(item['drugbank_id']).upper()}"
                for item in candidates
            ],
        )
        if not candidates:
            return _online_result(
                ConnectorStatus.NO_RESULTS,
                artifacts=(artifact,),
                mappings=(
                    IdentifierMapping(
                        input_identifier=name,
                        namespace="drug_name",
                        mapping_method="drugbank_name_search",
                        warning="No DrugBank drug matched the supplied name.",
                    ),
                ),
            )

        mapping_candidates = tuple(
            IdentifierCandidate(
                identifier=str(item["drugbank_id"]).upper(),
                label=_string_or_none(item.get("name")),
            )
            for item in candidates[:max_results]
        )
        exact_matches = [
            item
            for item in candidates
            if str(item.get("name", "")).strip().casefold() == name.casefold()
        ]
        selected: Mapping[str, Any] | None = None
        if len(exact_matches) == 1:
            selected = exact_matches[0]
        elif len(candidates) == 1:
            selected = candidates[0]
        if selected is None:
            return _online_result(
                ConnectorStatus.DEGRADED,
                artifacts=(artifact,),
                mappings=(
                    IdentifierMapping(
                        input_identifier=name,
                        namespace="drug_name",
                        candidates=mapping_candidates,
                        ambiguous=True,
                        mapping_method="drugbank_name_search",
                        warning=(
                            "The name maps to multiple DrugBank records; "
                            "explicit DrugBank ID selection is required."
                        ),
                    ),
                ),
                warnings=(
                    "DrugBank name resolution was ambiguous; VetEvidence "
                    "did not automatically select the first result.",
                ),
            )

        selected_id = str(selected["drugbank_id"]).upper()
        mapping = IdentifierMapping(
            input_identifier=name,
            namespace="drug_name",
            canonical_identifier=selected_id,
            candidates=mapping_candidates,
            mapping_method="drugbank_name_search_exact_or_unique",
        )
        return selected_id, artifact, mapping

    def _fetch_bonds(
        self,
        drugbank_id: str,
        *,
        max_results: int,
    ) -> tuple[
        ConnectorStatus,
        list[dict[str, Any]],
        tuple[ResponseArtifact, ...],
        tuple[str, ...],
    ]:
        endpoint = (
            f"{self.base_url}/drugs/{quote(drugbank_id, safe='')}/bonds"
        )
        try:
            artifact = self._request(
                "GET",
                endpoint,
                params={"per_page": max_results},
                headers={
                    "Accept": "application/json",
                    "Authorization": self.api_key,
                },
            )
        except ConnectorTransportError:
            return (
                ConnectorStatus.DEGRADED,
                [],
                (),
                ("DrugBank transport failed while retrieving bonds.",),
            )
        http_status = artifact.provenance.http_status
        if http_status in {204, 404}:
            return (
                ConnectorStatus.OK,
                [],
                (artifact,),
                ("DrugBank returned no bonds.",),
            )
        if http_status is not None and http_status >= 400:
            return (
                ConnectorStatus.DEGRADED,
                [],
                (artifact,),
                (f"DrugBank bond endpoint returned HTTP {http_status}.",),
            )
        try:
            payload = _payload_json(artifact)
            raw_bonds = _extract_drugbank_bonds(payload)
            records = [
                _normalize_drugbank_bond(
                    value,
                    drugbank_id=drugbank_id,
                )
                for value in raw_bonds[:max_results]
            ]
        except ConnectorError:
            return (
                ConnectorStatus.DEGRADED,
                [],
                (artifact,),
                (
                    "DrugBank bond JSON did not match the documented "
                    "schema.",
                ),
            )

        bio_entity_ids = list(
            dict.fromkeys(
                str(record["bio_entity_id"])
                for record in records
            )
        )
        artifact = _update_provenance(
            artifact,
            stable_ids=[
                f"DrugBankBioEntity:{bio_entity_id}"
                for bio_entity_id in bio_entity_ids
            ],
        )
        artifacts = [artifact]
        warnings: list[str] = []
        status = ConnectorStatus.OK

        total_count = _non_negative_header_int(
            artifact.response_headers.get("x-total-count")
        )
        if total_count is not None and total_count > len(records):
            status = ConnectorStatus.DEGRADED
            warnings.append(
                "DrugBank returned only the first "
                f"{len(records)}/{total_count} bonds; additional pages were "
                "not retrieved."
            )

        if not records:
            warnings.append("DrugBank returned no bonds.")
            return status, records, tuple(artifacts), tuple(warnings)

        detail_endpoint = f"{self.base_url}/bio_entities"
        try:
            detail_artifact = self._request(
                "GET",
                detail_endpoint,
                params={"ids": ",".join(bio_entity_ids)},
                headers={
                    "Accept": "application/json",
                    "Authorization": self.api_key,
                },
            )
        except ConnectorTransportError:
            warnings.append(
                "DrugBank transport failed while retrieving bio-entity "
                "details; bond records were kept without added "
                "identifiers."
            )
            return (
                ConnectorStatus.DEGRADED,
                records,
                tuple(artifacts),
                tuple(warnings),
            )

        artifacts.append(detail_artifact)
        detail_http_status = detail_artifact.provenance.http_status
        if detail_http_status is not None and detail_http_status >= 400:
            warnings.append(
                "DrugBank bio-entity detail endpoint returned HTTP "
                f"{detail_http_status}; bond records were kept without "
                "added identifiers."
            )
            return (
                ConnectorStatus.DEGRADED,
                records,
                tuple(artifacts),
                tuple(warnings),
            )

        try:
            detail_payload = _payload_json(detail_artifact)
            raw_details = _extract_drugbank_bio_entities(detail_payload)
            normalized_details = [
                _normalize_drugbank_bio_entity(value)
                for value in raw_details
            ]
            detail_by_id: dict[str, dict[str, Any]] = {}
            for detail in normalized_details:
                detail_id = str(detail["bio_entity_id"])
                if detail_id in detail_by_id:
                    raise ConnectorError(
                        "DrugBank returned a duplicate bio-entity ID."
                    )
                detail_by_id[detail_id] = detail
        except ConnectorError:
            warnings.append(
                "DrugBank bio-entity detail JSON did not match the "
                "documented schema; bond records were kept without "
                "added identifiers."
            )
            return (
                ConnectorStatus.DEGRADED,
                records,
                tuple(artifacts),
                tuple(warnings),
            )

        detail_stable_ids: list[str] = []
        for detail in normalized_details:
            detail_stable_ids.append(
                f"DrugBankBioEntity:{detail['bio_entity_id']}"
            )
            if detail.get("taxon_id"):
                detail_stable_ids.append(
                    f"NCBITaxon:{detail['taxon_id']}"
                )
            detail_stable_ids.extend(
                f"UniProtKB:{uniprot_id}"
                for uniprot_id in detail["uniprot_ids"]
            )
        detail_artifact = _update_provenance(
            detail_artifact,
            stable_ids=detail_stable_ids,
        )
        artifacts[-1] = detail_artifact

        enriched_records = [
            _merge_drugbank_bond_detail(
                record,
                detail_by_id.get(str(record["bio_entity_id"])),
            )
            for record in records
        ]
        missing_ids = [
            bio_entity_id
            for bio_entity_id in bio_entity_ids
            if bio_entity_id not in detail_by_id
        ]
        if missing_ids:
            status = ConnectorStatus.DEGRADED
            warnings.append(
                "DrugBank returned bio-entity details for only "
                f"{len(bio_entity_ids) - len(missing_ids)}/"
                f"{len(bio_entity_ids)} requested IDs; unmatched bond "
                "records were kept without added identifiers."
            )
        return status, enriched_records, tuple(artifacts), tuple(warnings)


def _extract_drugbank_search_candidates(
    payload: Any,
) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        values = _mapping_list(payload, "DrugBank search response")
    elif isinstance(payload, Mapping):
        envelope = payload.get("data", payload.get("results"))
        values = _mapping_list(envelope, "DrugBank search results")
    else:
        raise ConnectorError("DrugBank search response must be a list.")
    for item in values:
        drugbank_id = str(item.get("drugbank_id", "")).upper()
        if not _DRUGBANK_ID_PATTERN.fullmatch(drugbank_id):
            raise ConnectorError("DrugBank search result has an invalid ID.")
    return values


def _normalize_drugbank_drug(
    payload: Any,
    *,
    expected_id: str,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise ConnectorError("DrugBank drug detail must be an object.")
    drugbank_id = str(payload.get("drugbank_id", "")).upper()
    if drugbank_id != expected_id:
        raise ConnectorError("DrugBank detail ID does not match the request.")
    name = _string_or_none(payload.get("name"))
    if not name:
        raise ConnectorError("DrugBank drug detail is missing the name.")
    identifiers = payload.get("identifiers")
    if identifiers is not None and not isinstance(identifiers, Mapping):
        raise ConnectorError("DrugBank identifiers must be an object.")
    identifiers = identifiers if isinstance(identifiers, Mapping) else {}
    return {
        "record_type": "drugbank_drug",
        "drugbank_id": drugbank_id,
        "name": name,
        "drug_type": _string_or_none(payload.get("type")),
        "annotation_status": _string_or_none(
            payload.get("annotation_status")
        ),
        "groups": _string_list(payload.get("groups")),
        "cas_number": _string_or_none(payload.get("cas_number")),
        "inchi": _string_or_none(identifiers.get("inchi")),
        "inchikey": _string_or_none(identifiers.get("inchikey")),
        "atc_codes": _normalize_atc_codes(identifiers.get("atc_codes")),
        "references": _normalize_references(payload.get("references")),
        "source_url": f"https://go.drugbank.com/drugs/{drugbank_id}",
    }


def _normalize_atc_codes(value: Any) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    values = _mapping_list(value, "DrugBank ATC codes")
    return [
        {
            "code": _string_or_none(item.get("code")),
            "title": _string_or_none(item.get("title")),
        }
        for item in values
        if item.get("code")
    ]


def _extract_drugbank_bonds(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return _mapping_list(payload, "DrugBank bonds response")
    if isinstance(payload, Mapping):
        values = payload.get("data", payload.get("bonds"))
        return _mapping_list(values, "DrugBank bonds")
    raise ConnectorError("DrugBank bonds response must be a list.")


def _extract_drugbank_bio_entities(
    payload: Any,
) -> list[Mapping[str, Any]]:
    if isinstance(payload, list):
        return _mapping_list(payload, "DrugBank bio-entity response")
    if isinstance(payload, Mapping):
        values = payload.get("data", payload.get("bio_entities"))
        return _mapping_list(values, "DrugBank bio-entities")
    raise ConnectorError("DrugBank bio-entity response must be a list.")


def _normalize_drugbank_bond(
    payload: Mapping[str, Any],
    *,
    drugbank_id: str,
) -> dict[str, Any]:
    bio_entity = payload.get("bio_entity")
    if not isinstance(bio_entity, Mapping):
        raise ConnectorError("DrugBank bond is missing bio_entity.")
    bio_entity_id = _string_or_none(
        bio_entity.get("bio_entity_id")
    )
    if (
        not bio_entity_id
        or not _DRUGBANK_BIO_ENTITY_ID_PATTERN.fullmatch(bio_entity_id)
    ):
        raise ConnectorError(
            "DrugBank bond bio_entity has no valid official ID."
        )
    bio_entity_id = bio_entity_id.upper()
    return {
        "record_type": "drugbank_bond",
        "drugbank_id": drugbank_id,
        "bond_type": _string_or_none(payload.get("type")),
        "bio_entity_id": bio_entity_id,
        "bio_entity_name": _string_or_none(bio_entity.get("name")),
        "organism": _string_or_none(bio_entity.get("organism")),
        "taxon_id": None,
        "uniprot_ids": [],
        "uniprot_id": None,
        "gene_symbols": [],
        "gene_symbol": None,
        "known_action": _string_or_none(payload.get("known_action")),
        "actions": _string_list(payload.get("actions")),
        "inhibition_strength": _string_or_none(
            payload.get("inhibition_strength")
        ),
        "induction_strength": _string_or_none(
            payload.get("induction_strength")
        ),
        "references": _normalize_references(payload.get("references")),
        "source_url": (
            f"https://go.drugbank.com/drugs/{drugbank_id}#bond-{bio_entity_id}"
        ),
    }


def _normalize_drugbank_bio_entity(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    bio_entity_id = _string_or_none(payload.get("id"))
    if (
        not bio_entity_id
        or not _DRUGBANK_BIO_ENTITY_ID_PATTERN.fullmatch(bio_entity_id)
    ):
        raise ConnectorError(
            "DrugBank bio-entity detail has no valid official ID."
        )
    polypeptide_value = payload.get("polypeptides", [])
    if polypeptide_value is None:
        polypeptide_value = []
    polypeptides = _mapping_list(
        polypeptide_value,
        "DrugBank bio-entity polypeptides",
    )
    uniprot_ids: list[str] = []
    gene_symbols: list[str] = []
    for polypeptide in polypeptides:
        primary_uniprot_id = _string_or_none(
            polypeptide.get("uniprot_id")
        )
        if primary_uniprot_id:
            uniprot_ids.append(primary_uniprot_id)
        uniprot_ids.extend(_string_list(polypeptide.get("uniprot_ids")))
        gene_name = _string_or_none(polypeptide.get("gene_name"))
        if gene_name:
            gene_symbols.append(gene_name)
    uniprot_ids = list(dict.fromkeys(uniprot_ids))
    gene_symbols = list(dict.fromkeys(gene_symbols))
    return {
        "bio_entity_id": bio_entity_id.upper(),
        "bio_entity_name": _string_or_none(payload.get("name")),
        "organism": _string_or_none(payload.get("organism")),
        "taxon_id": _taxon_id(payload.get("ncbi_taxonomy_id")),
        "uniprot_ids": uniprot_ids,
        "gene_symbols": gene_symbols,
    }


def _merge_drugbank_bond_detail(
    bond: Mapping[str, Any],
    detail: Mapping[str, Any] | None,
) -> dict[str, Any]:
    merged = dict(bond)
    if detail is None:
        return merged
    if detail.get("bio_entity_name"):
        merged["bio_entity_name"] = detail["bio_entity_name"]
    if detail.get("organism"):
        merged["organism"] = detail["organism"]
    merged["taxon_id"] = detail.get("taxon_id")
    uniprot_ids = list(detail.get("uniprot_ids", []))
    gene_symbols = list(detail.get("gene_symbols", []))
    merged["uniprot_ids"] = uniprot_ids
    merged["uniprot_id"] = uniprot_ids[0] if uniprot_ids else None
    merged["gene_symbols"] = gene_symbols
    merged["gene_symbol"] = gene_symbols[0] if gene_symbols else None
    return merged


def _normalize_references(value: Any) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if isinstance(value, Mapping):
        candidates = value.get(
            "literature_references",
            value.get("references", []),
        )
    else:
        candidates = value
    references = _mapping_list(candidates, "DrugBank literature references")
    normalized: list[dict[str, Any]] = []
    for reference in references:
        pmid = reference.get("pubmed_id", reference.get("pmid"))
        normalized.append(
            {
                "reference_id": _string_or_none(
                    reference.get("ref_id", reference.get("id"))
                ),
                "pmid": _string_or_none(pmid),
                "citation": _string_or_none(reference.get("citation")),
            }
        )
    return [
        item
        for item in normalized
        if any(value is not None for value in item.values())
    ]


def _taxon_id(value: Any) -> int | None:
    if value is None:
        return None
    match = re.search(r"(\d+)$", str(value).strip())
    if not match:
        return None
    parsed = int(match.group(1))
    return parsed if parsed > 0 else None


def _non_negative_header_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _string_or_none(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


__all__ = [
    "DrugBankConnector",
    "OMIMConnector",
]
