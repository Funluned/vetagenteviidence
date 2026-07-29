from __future__ import annotations

import httpx
import pytest

from vetevidence.config import Settings
from vetevidence.pubmed import PubMedClient, PubMedError


SEARCH_RESULT = b'{"esearchresult":{"idlist":["42250334"]}}'
EMPTY_SEARCH_RESULT = b'{"esearchresult":{"idlist":[]}}'
FETCH_RESULT = b"""<?xml version="1.0"?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>42250334</PMID>
      <Article>
        <Journal>
          <JournalIssue>
            <PubDate><Year>2026</Year></PubDate>
          </JournalIssue>
          <Title>Research in Veterinary Science</Title>
          <ISSN IssnType="Electronic">1532-2661</ISSN>
        </Journal>
        <ArticleTitle>Quercetin and <i>Streptococcus agalactiae</i></ArticleTitle>
        <Abstract>
          <AbstractText Label="RESULTS">Inflammation was reduced.</AbstractText>
          <AbstractText Label="CONCLUSION">The pathway was inhibited.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author>
            <LastName>Sun</LastName>
            <ForeName>Qi</ForeName>
          </Author>
        </AuthorList>
      </Article>
      <MedlineJournalInfo>
        <ISSNLinking>0034-5288</ISSNLinking>
      </MedlineJournalInfo>
    </MedlineCitation>
    <PubmedData>
      <ArticleIdList>
        <ArticleId IdType="doi">10.1016/j.rvsc.2026.106289</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
</PubmedArticleSet>
"""


def test_search_parses_traceable_article_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("esearch.fcgi"):
            return httpx.Response(200, content=SEARCH_RESULT, request=request)
        return httpx.Response(200, content=FETCH_RESULT, request=request)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        client = PubMedClient(client=http_client)
        articles = client.search("quercetin mastitis", max_results=1)

    assert len(articles) == 1
    article = articles[0]
    assert article.pmid == "42250334"
    assert article.doi == "10.1016/j.rvsc.2026.106289"
    assert article.year == 2026
    assert article.authors == ["Qi Sun"]
    assert article.issn == "1532-2661"
    assert article.issn_type == "Electronic"
    assert article.issn_linking == "0034-5288"
    assert "Streptococcus agalactiae" in article.title
    assert "RESULTS: Inflammation was reduced." in article.abstract
    assert article.source_url.endswith("/42250334/")


def test_empty_search_does_not_fetch_details() -> None:
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(200, content=EMPTY_SEARCH_RESULT, request=request)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        articles = PubMedClient(client=http_client).search("no results")

    assert articles == []
    assert request_count == 1


def test_blank_query_is_rejected() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        PubMedClient().search("   ")


def test_http_failure_becomes_user_facing_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        with pytest.raises(PubMedError, match="NCBI"):
            PubMedClient(
                settings=Settings(max_retries=0),
                client=http_client,
            ).search("mastitis")


def test_retryable_http_failure_is_retried() -> None:
    search_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal search_attempts
        if request.url.path.endswith("esearch.fcgi"):
            search_attempts += 1
            if search_attempts == 1:
                return httpx.Response(503, request=request)
            return httpx.Response(200, content=SEARCH_RESULT, request=request)
        return httpx.Response(200, content=FETCH_RESULT, request=request)

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as http_client:
        client = PubMedClient(
            settings=Settings(
                max_retries=1,
                retry_backoff_seconds=0,
            ),
            client=http_client,
        )
        articles = client.search("mastitis")

    assert search_attempts == 2
    assert client.request_count == 3
    assert articles[0].pmid == "42250334"
