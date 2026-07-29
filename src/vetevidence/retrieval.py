from __future__ import annotations

from vetevidence.journal_rankings import (
    JournalRankingProvider,
    LetPubJournalRankingProvider,
)
from vetevidence.models import ResearchResult
from vetevidence.providers import EvidenceProvider, RuleBasedEvidenceProvider
from vetevidence.pubmed import PubMedClient


def run_research(
    query: str,
    max_results: int = 5,
    *,
    client: PubMedClient | None = None,
    provider: EvidenceProvider | None = None,
    ranking_provider: JournalRankingProvider | None = None,
) -> ResearchResult:
    """Run retrieval, extraction and cited answering as one testable workflow."""
    active_client = client or PubMedClient()
    active_provider = provider or RuleBasedEvidenceProvider()
    active_ranking_provider = (
        ranking_provider or LetPubJournalRankingProvider.default()
    )
    owns_client = client is None
    owns_ranking_provider = ranking_provider is None

    try:
        raw_articles = active_client.search(query, max_results=max_results)
        rankings = active_ranking_provider.lookup_many(raw_articles)
        articles = [
            article.model_copy(
                update={"journal_ranking": ranking}
            )
            for article, ranking in zip(raw_articles, rankings, strict=True)
        ]
        evidence = [active_provider.extract(article) for article in articles]
        answer = active_provider.answer(query, evidence)
        return ResearchResult(
            query=query,
            articles=articles,
            evidence=evidence,
            answer=answer,
            provider_name=active_provider.name,
            retrieval_request_count=getattr(
                active_client,
                "request_count",
                0,
            ),
            estimated_llm_cost_usd=0.0,
        )
    finally:
        if owns_client:
            active_client.close()
        if owns_ranking_provider:
            active_ranking_provider.close()
