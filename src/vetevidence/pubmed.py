from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable

import httpx

from vetevidence.config import Settings
from vetevidence.models import PubMedArticle


ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
logger = logging.getLogger(__name__)


class PubMedError(RuntimeError):
    """A user-facing PubMed retrieval or parsing failure."""


class PubMedClient:
    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self._owns_client = client is None
        self.request_count = 0
        self._client = client or httpx.Client(
            timeout=self.settings.request_timeout_seconds,
            headers={"User-Agent": self.settings.user_agent},
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _get(
        self,
        url: str,
        params: dict[str, object],
    ) -> httpx.Response:
        attempts = self.settings.max_retries + 1
        last_error: httpx.RequestError | None = None

        for attempt in range(attempts):
            self.request_count += 1
            try:
                response = self._client.get(url, params=params)
            except httpx.RequestError as exc:
                last_error = exc
                if attempt + 1 >= attempts:
                    raise
                logger.warning(
                    "NCBI request failed; retrying",
                    extra={"attempt": attempt + 1, "url": url},
                )
            else:
                if (
                    response.status_code not in RETRYABLE_STATUS_CODES
                    or attempt + 1 >= attempts
                ):
                    return response
                logger.warning(
                    "NCBI returned retryable status; retrying",
                    extra={
                        "attempt": attempt + 1,
                        "status_code": response.status_code,
                        "url": url,
                    },
                )

            time.sleep(
                self.settings.retry_backoff_seconds * (2**attempt)
            )

        if last_error:
            raise last_error
        raise RuntimeError("NCBI request retry loop exited unexpectedly.")

    def search(self, query: str, max_results: int = 10) -> list[PubMedArticle]:
        clean_query = query.strip()
        if not clean_query:
            raise ValueError("检索词不能为空。")
        if not 1 <= max_results <= 100:
            raise ValueError("max_results 必须在 1 到 100 之间。")

        common_params = {
            "tool": self.settings.ncbi_tool,
            "email": self.settings.ncbi_email,
            "api_key": self.settings.ncbi_api_key,
        }
        search_params = {
            "db": "pubmed",
            "term": clean_query,
            "retmode": "json",
            "retmax": max_results,
            "sort": "relevance",
            **common_params,
        }

        try:
            response = self._get(
                ESEARCH_URL,
                _without_none(search_params),
            )
            response.raise_for_status()
            payload = response.json()
            pmids = payload["esearchresult"]["idlist"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise PubMedError("无法从 NCBI 获取有效的检索结果。") from exc

        if not pmids:
            return []

        fetch_params = {
            "db": "pubmed",
            "id": ",".join(pmids),
            "rettype": "abstract",
            "retmode": "xml",
            **common_params,
        }
        try:
            response = self._get(
                EFETCH_URL,
                _without_none(fetch_params),
            )
            response.raise_for_status()
            articles = _parse_pubmed_xml(response.text)
        except httpx.HTTPError as exc:
            raise PubMedError("文献编号已找到，但摘要和元数据获取失败。") from exc
        except ET.ParseError as exc:
            raise PubMedError("NCBI 返回了无法解析的文献数据。") from exc

        by_pmid = {article.pmid: article for article in articles}
        return [by_pmid[pmid] for pmid in pmids if pmid in by_pmid]


def _without_none(params: dict[str, object | None]) -> dict[str, object]:
    return {key: value for key, value in params.items() if value is not None}


def _node_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return " ".join("".join(node.itertext()).split())


def _first_text(root: ET.Element, paths: Iterable[str]) -> str | None:
    for path in paths:
        value = _node_text(root.find(path))
        if value:
            return value
    return None


def _parse_year(article_node: ET.Element) -> int | None:
    value = _first_text(
        article_node,
        (
            "./MedlineCitation/Article/ArticleDate/Year",
            "./MedlineCitation/Article/Journal/JournalIssue/PubDate/Year",
            "./MedlineCitation/Article/Journal/JournalIssue/PubDate/MedlineDate",
        ),
    )
    if not value:
        return None
    match = re.search(r"\b(18|19|20|21)\d{2}\b", value)
    return int(match.group(0)) if match else None


def _parse_authors(article_node: ET.Element) -> list[str]:
    authors: list[str] = []
    author_nodes = article_node.findall(
        "./MedlineCitation/Article/AuthorList/Author"
    )
    for author in author_nodes:
        collective = _node_text(author.find("./CollectiveName"))
        if collective:
            authors.append(collective)
            continue

        last_name = _node_text(author.find("./LastName"))
        given_name = _first_text(author, ("./ForeName", "./Initials")) or ""
        full_name = " ".join(part for part in (given_name, last_name) if part)
        if full_name:
            authors.append(full_name)
    return authors


def _parse_abstract(article_node: ET.Element) -> str | None:
    sections: list[str] = []
    abstract_nodes = article_node.findall(
        "./MedlineCitation/Article/Abstract/AbstractText"
    )
    for node in abstract_nodes:
        text = _node_text(node)
        if not text:
            continue
        label = (node.attrib.get("Label") or "").strip()
        sections.append(f"{label}: {text}" if label else text)
    return "\n\n".join(sections) or None


def _parse_doi(article_node: ET.Element) -> str | None:
    for article_id in article_node.findall(
        "./PubmedData/ArticleIdList/ArticleId"
    ):
        if article_id.attrib.get("IdType", "").lower() == "doi":
            value = _node_text(article_id)
            if value:
                return value
    return None


def _parse_pubmed_xml(xml_text: str) -> list[PubMedArticle]:
    root = ET.fromstring(xml_text)
    articles: list[PubMedArticle] = []

    for article_node in root.findall("./PubmedArticle"):
        pmid = _first_text(article_node, ("./MedlineCitation/PMID",))
        if not pmid:
            continue

        title = _first_text(
            article_node,
            ("./MedlineCitation/Article/ArticleTitle",),
        )
        journal = _first_text(
            article_node,
            (
                "./MedlineCitation/Article/Journal/Title",
                "./MedlineCitation/MedlineJournalInfo/MedlineTA",
            ),
        )
        issn_node = article_node.find(
            "./MedlineCitation/Article/Journal/ISSN"
        )
        issn = _node_text(issn_node) or None
        issn_type = (
            issn_node.attrib.get("IssnType")
            if issn_node is not None
            else None
        )
        issn_linking = _first_text(
            article_node,
            ("./MedlineCitation/MedlineJournalInfo/ISSNLinking",),
        )
        articles.append(
            PubMedArticle(
                pmid=pmid,
                title=title or "标题未报告",
                authors=_parse_authors(article_node),
                journal=journal,
                issn=issn,
                issn_type=issn_type,
                issn_linking=issn_linking,
                year=_parse_year(article_node),
                doi=_parse_doi(article_node),
                abstract=_parse_abstract(article_node),
                source_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            )
        )

    return articles
