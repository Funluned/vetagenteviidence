from __future__ import annotations

from pathlib import Path

import httpx

from vetevidence.journal_rankings import (
    CsvJournalRankingProvider,
    LetPubJournalRankingProvider,
    _parse_cas_partition,
    _parse_jcr_partition,
    _parse_search_result,
)
from vetevidence.models import PubMedArticle


def article(
    journal: str,
    *,
    issn: str | None = None,
    issn_linking: str | None = None,
) -> PubMedArticle:
    return PubMedArticle(
        pmid="1",
        title="Test article",
        journal=journal,
        issn=issn,
        issn_linking=issn_linking,
        source_url="https://pubmed.ncbi.nlm.nih.gov/1/",
    )


def test_ranking_matches_by_issn_and_keeps_both_standards() -> None:
    ranking = CsvJournalRankingProvider.default().lookup(
        article(
            "Different title spelling",
            issn="1532-2661",
            issn_linking="0034-5288",
        )
    )

    assert ranking.matched_by == "ISSN 0034-5288"
    assert ranking.cas_edition == "2025年3月升级版"
    assert ranking.cas_large_zone == "3区"
    assert "兽医学 3区" in ranking.cas_display()
    assert ranking.jcr_edition == "2025-2026（JIF）"
    assert ranking.jcr_categories[0].quartile == "Q2"
    assert "VETERINARY SCIENCES Q2（JIF SCIE，54/170）" in ranking.jcr_display()


def test_multiple_categories_are_not_collapsed() -> None:
    ranking = CsvJournalRankingProvider.default().lookup(
        article("Animals", issn="2076-2615")
    )

    assert len(ranking.cas_categories) == 2
    assert len(ranking.jcr_categories) == 2
    assert "奶制品与动物科学" in ranking.cas_display()
    assert "兽医学" in ranking.cas_display()
    assert "AGRICULTURE, DAIRY & ANIMAL SCIENCE Q1" in ranking.jcr_display()
    assert "VETERINARY SCIENCES Q1" in ranking.jcr_display()


def test_unknown_journal_is_explicitly_unrecorded() -> None:
    ranking = CsvJournalRankingProvider.default().lookup(
        article("Unknown Journal", issn="0000-0000")
    )

    assert ranking.data_status == "not_found"
    assert ranking.cas_display() == "未收录（中科院 2025年3月升级版）"
    assert ranking.jcr_display() == "未收录（JCR 2025-2026（JIF））"
    assert "系统未推断分区" in ranking.source_note


SEARCH_HTML = """
<table>
  <tr>
    <td>0034-5288</td>
    <td><a href="index.php?journalid=7153&page=journalapp&view=detail">
      Research in Veterinary Science
    </a></td>
  </tr>
</table>
"""

CAS_HTML = """
<h3>2025年3月升级版</h3>
<table>
  <tr>
    <th>大类学科</th><th>小类学科</th><th>Top期刊</th><th>综述期刊</th>
  </tr>
  <tr>
    <td>农林科学
      <span style="display:none">1区</span><span>3区</span>
    </td>
    <td><table><tr>
      <td>VETERINARY SCIENCES<br>兽医学</td><td><span>3区</span></td>
    </tr></table></td>
    <td>否</td><td>否</td>
  </tr>
</table>
<h3>2023年12月升级版</h3>
"""

JCR_HTML = """
<div>Self-citation (2025-2026)</div>
<h3>Quartiles By JIF</h3>
<table>
  <tr>
    <td>Category: VETERINARY SCIENCES</td>
    <td>SCIE</td><td>Q2</td><td>54/170</td>
  </tr>
</table>
<h3>Quartiles By JCI</h3>
"""


def test_letpub_html_parsers_keep_exact_standard_and_rank() -> None:
    assert _parse_search_result(SEARCH_HTML, "0034-5288") == (
        "7153",
        "Research in Veterinary Science",
    )

    cas_edition, large_category, large_zone, cas_categories = (
        _parse_cas_partition(CAS_HTML)
    )
    assert cas_edition == "2025年3月升级版"
    assert large_category.endswith("农林科学")
    assert large_zone == "3区"
    assert cas_categories[0].category == "VETERINARY SCIENCES / 兽医学"
    assert cas_categories[0].zone == "3区"

    jcr_edition, jcr_categories = _parse_jcr_partition(JCR_HTML)
    assert jcr_edition == "2025-2026（JIF）"
    assert jcr_categories[0].quartile == "Q2"
    assert jcr_categories[0].collection == "SCIE"
    assert jcr_categories[0].rank == "54/170"


def test_letpub_provider_caches_live_lookup(
    tmp_path: Path,
) -> None:
    requested_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        if "searchissn=" in str(request.url):
            return httpx.Response(200, text=SEARCH_HTML)
        if request.url.host == "www.letpub.com.cn":
            return httpx.Response(200, text=CAS_HTML)
        return httpx.Response(200, text=JCR_HTML)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = LetPubJournalRankingProvider(
        client=client,
        cache_path=tmp_path / "letpub.json",
    )
    target = article(
        "Research in Veterinary Science",
        issn="1532-2661",
        issn_linking="0034-5288",
    )

    live = provider.lookup(target)
    cached = provider.lookup(target)

    assert live.data_status == "available"
    assert cached.data_status == "cached"
    assert cached.jcr_categories[0].rank == "54/170"
    assert len(requested_urls) == 3
    assert (tmp_path / "letpub.json").is_file()
    client.close()


def test_letpub_failure_uses_local_fallback(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = LetPubJournalRankingProvider(
        client=client,
        cache_path=tmp_path / "letpub.json",
    )

    ranking = provider.lookup(
        article(
            "Research in Veterinary Science",
            issn_linking="0034-5288",
        )
    )

    assert ranking.data_status == "fallback"
    assert ranking.cas_large_zone == "3区"
    assert "LetPub 本次查询失败" in ranking.source_note
    client.close()
