from __future__ import annotations

import csv
import html
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

import httpx

from vetevidence.models import (
    CASCategoryPartition,
    JCRCategoryPartition,
    JournalRanking,
    PubMedArticle,
)


DEFAULT_CAS_EDITION = "2025年3月升级版"
DEFAULT_JCR_EDITION = "2025-2026（JIF）"
LETPUB_SEARCH_URL = "https://www.letpub.com.cn/index.php"
LETPUB_CN_DETAIL_URL = (
    "https://www.letpub.com.cn/index.php"
    "?journalid={journal_id}&page=journalapp&view=detail"
)
LETPUB_EN_DETAIL_URL = (
    "https://www.letpub.com/journal-selector/journal/{journal_id}"
)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class JournalRankingProvider(Protocol):
    def lookup(self, article: PubMedArticle) -> JournalRanking: ...

    def lookup_many(
        self,
        articles: list[PubMedArticle],
    ) -> list[JournalRanking]: ...

    def close(self) -> None: ...


class LetPubLookupError(RuntimeError):
    """LetPub lookup failed without breaking the PubMed research workflow."""


def _normalize_issn(value: str | None) -> str:
    return re.sub(r"[^0-9X]", "", (value or "").upper())


def _normalize_title(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


def _append_unique(items: list[object], candidate: object) -> None:
    if candidate not in items:
        items.append(candidate)


def _clean_cell(value: str) -> str:
    value = re.sub(r"<(?:script|style)\b.*?</(?:script|style)>", "", value, flags=re.I | re.S)
    value = re.sub(r"<br\s*/?>", " / ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    return " ".join(html.unescape(value).replace("\xa0", " ").split())


def _visible_lines(fragment: str) -> list[str]:
    value = re.sub(
        r"<span\b[^>]*display\s*:\s*none[^>]*>.*?</span>",
        "",
        fragment,
        flags=re.I | re.S,
    )
    value = re.sub(
        r"<(?:script|style)\b.*?</(?:script|style)>",
        "",
        value,
        flags=re.I | re.S,
    )
    value = re.sub(
        r"</?(?:table|tr|td|th|br)\b[^>]*>",
        "\n",
        value,
        flags=re.I,
    )
    value = re.sub(r"<[^>]+>", "", value)
    return [
        " ".join(line.split())
        for line in html.unescape(value).replace("\xa0", " ").splitlines()
        if line.strip()
    ]


def _parse_search_result(
    html_text: str,
    expected_issn: str | None,
) -> tuple[str, str] | None:
    normalized_expected = _normalize_issn(expected_issn)
    candidates: list[tuple[str, str, str]] = []
    for row in re.findall(r"<tr\b[^>]*>.*?</tr>", html_text, flags=re.I | re.S):
        match = re.search(
            r"journalid=(\d+)[^\"']*[\"'][^>]*>(.*?)</a>",
            row,
            flags=re.I | re.S,
        )
        if not match:
            continue
        cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, flags=re.I | re.S)
        row_issn = _clean_cell(cells[0]) if cells else ""
        journal_id = match.group(1)
        journal_title = _clean_cell(match.group(2))
        candidates.append((row_issn, journal_id, journal_title))

    if normalized_expected:
        for row_issn, journal_id, journal_title in candidates:
            if _normalize_issn(row_issn) == normalized_expected:
                return journal_id, journal_title
        return None

    if candidates:
        _, journal_id, journal_title = candidates[0]
        return journal_id, journal_title
    return None


def _parse_cas_partition(
    html_text: str,
) -> tuple[str, str | None, str | None, list[CASCategoryPartition]]:
    edition_match = re.search(
        r"(2025年3月(?:最新)?升级版)",
        html_text,
        flags=re.I,
    )
    if not edition_match:
        return DEFAULT_CAS_EDITION, None, None, []

    start = edition_match.start()
    end_match = re.search(
        r"2023年12月.*?升级版",
        html_text[edition_match.end() :],
        flags=re.I | re.S,
    )
    end = (
        edition_match.end() + end_match.start()
        if end_match
        else min(len(html_text), start + 20_000)
    )
    lines = _visible_lines(html_text[start:end])

    try:
        payload_start = lines.index("综述期刊") + 1
    except ValueError:
        return edition_match.group(1), None, None, []

    payload = lines[payload_start:]
    large_match = (
        re.fullmatch(r"(.+?)\s*([1-4]区)", payload[0])
        if payload
        else None
    )
    if large_match:
        large_category = large_match.group(1).strip()
        large_zone = large_match.group(2)
        category_payload = payload[1:]
    else:
        category_payload = payload
        first_zone_index = next(
            (
                index
                for index, value in enumerate(payload)
                if re.fullmatch(r"[1-4]区", value)
            ),
            None,
        )
        if first_zone_index is None or first_zone_index == 0:
            return edition_match.group(1), None, None, []
        large_category = " / ".join(payload[:first_zone_index])
        large_zone = payload[first_zone_index]
        category_payload = payload[first_zone_index + 1 :]

    categories: list[CASCategoryPartition] = []
    category_parts: list[str] = []

    for value in category_payload:
        if value in {"否", "是", "N/A"}:
            break
        if re.fullmatch(r"[1-4]区", value):
            if category_parts:
                categories.append(
                    CASCategoryPartition(
                        category=" / ".join(category_parts),
                        zone=value,
                    )
                )
                category_parts = []
            continue
        category_parts.append(value)

    return edition_match.group(1), large_category, large_zone, categories


def _parse_jcr_partition(
    html_text: str,
) -> tuple[str, list[JCRCategoryPartition]]:
    start = html_text.find("Quartiles By JIF")
    end = html_text.find("Quartiles By JCI", start + 1)
    if start < 0:
        return DEFAULT_JCR_EDITION, []
    if end < 0:
        end = min(len(html_text), start + 20_000)

    edition_match = re.search(
        r"Self-citation\s*\((\d{4}-\d{4})\)",
        html_text,
        flags=re.I,
    )
    edition = (
        f"{edition_match.group(1)}（JIF）"
        if edition_match
        else DEFAULT_JCR_EDITION
    )

    categories: list[JCRCategoryPartition] = []
    for row in re.findall(
        r"<tr\b[^>]*>(.*?)</tr>",
        html_text[start:end],
        flags=re.I | re.S,
    ):
        cells = [
            _clean_cell(cell)
            for cell in re.findall(
                r"<td\b[^>]*>(.*?)</td>",
                row,
                flags=re.I | re.S,
            )
        ]
        if len(cells) < 4 or not cells[0].startswith("Category:"):
            continue
        quartile = cells[2].upper()
        if not re.fullmatch(r"Q[1-4]", quartile):
            continue
        categories.append(
            JCRCategoryPartition(
                category=cells[0].removeprefix("Category:").strip(),
                collection=cells[1] or None,
                quartile=quartile,
                rank=cells[3] or None,
                metric="JIF",
            )
        )
    return edition, categories


def _article_key(article: PubMedArticle) -> str:
    for value in (article.issn_linking, article.issn):
        normalized = _normalize_issn(value)
        if normalized:
            return f"issn:{normalized}"
    return f"title:{_normalize_title(article.journal)}"


class CsvJournalRankingProvider:
    """Load manually verified or institution-authorized fallback rankings."""

    def __init__(self, csv_path: Path) -> None:
        if not csv_path.is_file():
            raise FileNotFoundError(f"期刊分区 CSV 不存在：{csv_path}")
        self.csv_path = csv_path
        self._by_issn: dict[str, JournalRanking] = {}
        self._by_title: dict[str, JournalRanking] = {}
        self._load()

    @classmethod
    def default(cls) -> CsvJournalRankingProvider:
        configured = os.getenv("JOURNAL_RANKINGS_CSV")
        if configured:
            return cls(Path(configured).expanduser().resolve())
        project_root = Path(__file__).resolve().parents[2]
        return cls(project_root / "data" / "journal_rankings.csv")

    def close(self) -> None:
        return None

    def lookup_many(
        self,
        articles: list[PubMedArticle],
    ) -> list[JournalRanking]:
        return [self.lookup(article) for article in articles]

    def _load(self) -> None:
        grouped: dict[str, dict[str, object]] = {}
        issns_by_group: dict[str, set[str]] = {}

        with self.csv_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                journal_title = (row.get("journal_title") or "").strip()
                if not journal_title:
                    continue
                group_key = _normalize_title(journal_title)
                if group_key not in grouped:
                    grouped[group_key] = {
                        "journal_title": journal_title,
                        "cas_edition": (
                            row.get("cas_edition") or DEFAULT_CAS_EDITION
                        ).strip(),
                        "cas_large_category": (
                            row.get("cas_large_category") or ""
                        ).strip()
                        or None,
                        "cas_large_zone": (
                            row.get("cas_large_zone") or ""
                        ).strip()
                        or None,
                        "cas_categories": [],
                        "cas_source_url": (
                            row.get("cas_source_url") or ""
                        ).strip()
                        or None,
                        "jcr_edition": (
                            row.get("jcr_edition") or DEFAULT_JCR_EDITION
                        ).strip(),
                        "jcr_categories": [],
                        "jcr_source_url": (
                            row.get("jcr_source_url") or ""
                        ).strip()
                        or None,
                        "source_note": (
                            row.get("source_note") or ""
                        ).strip()
                        or None,
                        "data_status": "fallback",
                    }
                    issns_by_group[group_key] = set()

                data = grouped[group_key]
                cas_category = (row.get("cas_small_category") or "").strip()
                cas_zone = (row.get("cas_small_zone") or "").strip()
                if cas_category and cas_zone:
                    _append_unique(
                        data["cas_categories"],
                        CASCategoryPartition(
                            category=cas_category,
                            zone=cas_zone,
                        ),
                    )

                jcr_category = (row.get("jcr_category") or "").strip()
                jcr_quartile = (row.get("jcr_quartile") or "").strip()
                if jcr_category and jcr_quartile:
                    _append_unique(
                        data["jcr_categories"],
                        JCRCategoryPartition(
                            category=jcr_category,
                            quartile=jcr_quartile,
                            collection=(row.get("jcr_collection") or "").strip()
                            or None,
                            rank=(row.get("jcr_rank") or "").strip() or None,
                            metric=(row.get("jcr_metric") or "JIF").strip(),
                        ),
                    )

                for field in ("issn", "eissn"):
                    normalized = _normalize_issn(row.get(field))
                    if normalized:
                        issns_by_group[group_key].add(normalized)

        for group_key, data in grouped.items():
            ranking = JournalRanking.model_validate(data)
            self._by_title[group_key] = ranking
            for issn in issns_by_group[group_key]:
                self._by_issn[issn] = ranking

    def lookup(self, article: PubMedArticle) -> JournalRanking:
        for value in (article.issn_linking, article.issn):
            normalized = _normalize_issn(value)
            if normalized and normalized in self._by_issn:
                return self._by_issn[normalized].model_copy(
                    update={"matched_by": f"ISSN {value}"}
                )

        title_key = _normalize_title(article.journal)
        if title_key and title_key in self._by_title:
            return self._by_title[title_key].model_copy(
                update={"matched_by": "期刊名称"}
            )

        return JournalRanking(
            journal_title=article.journal or "期刊名未报告",
            matched_by=None,
            cas_edition=DEFAULT_CAS_EDITION,
            jcr_edition=DEFAULT_JCR_EDITION,
            source_note=(
                "LetPub 未返回可用记录，本地回退表也未收录该期刊；"
                "系统未推断分区。"
            ),
            data_status="not_found",
        )


class LetPubJournalRankingProvider:
    """Resolve rankings from LetPub public pages with a local JSON cache."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        fallback: JournalRankingProvider | None = None,
        cache_path: Path | None = None,
        cache_ttl_days: int = 7,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=12,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 VetEvidence-AI/0.1"
                )
            },
        )
        self.fallback = fallback or CsvJournalRankingProvider.default()
        project_root = Path(__file__).resolve().parents[2]
        self.cache_path = cache_path or (
            project_root / "data" / "cache" / "letpub_rankings.json"
        )
        self.cache_ttl = timedelta(days=max(1, cache_ttl_days))
        self.request_count = 0
        self._cache_lock = threading.Lock()
        self._cache = self._load_cache()

    @classmethod
    def default(cls) -> JournalRankingProvider:
        fallback = CsvJournalRankingProvider.default()
        enabled = os.getenv("LETPUB_LOOKUP_ENABLED", "true").strip().lower()
        if enabled in {"0", "false", "no", "off"}:
            return fallback
        try:
            ttl_days = int(os.getenv("LETPUB_CACHE_TTL_DAYS", "7"))
        except ValueError:
            ttl_days = 7
        return cls(fallback=fallback, cache_ttl_days=ttl_days)

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def lookup_many(
        self,
        articles: list[PubMedArticle],
    ) -> list[JournalRanking]:
        if not articles:
            return []

        unique_articles: dict[str, PubMedArticle] = {}
        for article in articles:
            unique_articles.setdefault(_article_key(article), article)

        with ThreadPoolExecutor(
            max_workers=min(4, len(unique_articles))
        ) as executor:
            rankings = dict(
                zip(
                    unique_articles,
                    executor.map(self.lookup, unique_articles.values()),
                    strict=True,
                )
            )
        return [rankings[_article_key(article)] for article in articles]

    def lookup(self, article: PubMedArticle) -> JournalRanking:
        cache_key = _article_key(article)
        cached = self._cached_ranking(cache_key, fresh_only=True)
        if cached:
            return cached

        try:
            ranking = self._lookup_live(article)
        except LetPubLookupError as exc:
            stale = self._cached_ranking(cache_key, fresh_only=False)
            if stale:
                return stale.model_copy(
                    update={
                        "data_status": "stale_cache",
                        "source_note": (
                            f"{stale.source_note or ''}；LetPub 本次查询失败，"
                            f"使用过期缓存：{exc}"
                        ).strip("；"),
                    }
                )
            fallback = self.fallback.lookup(article)
            return fallback.model_copy(
                update={
                    "source_note": (
                        f"LetPub 本次查询失败，已使用本地回退数据：{exc}；"
                        f"{fallback.source_note or ''}"
                    ).strip("；"),
                }
            )

        self._store_cache(cache_key, ranking)
        return ranking

    def _get_text(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
    ) -> str:
        last_error: Exception | None = None
        for attempt in range(2):
            self.request_count += 1
            try:
                response = self._client.get(url, params=params)
                if response.status_code in RETRYABLE_STATUS_CODES:
                    response.raise_for_status()
                response.raise_for_status()
                return response.text
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(0.25)
        raise LetPubLookupError(
            f"无法访问 LetPub（{type(last_error).__name__}）"
        ) from last_error

    def _find_journal(self, article: PubMedArticle) -> tuple[str, str, str]:
        for value in (article.issn_linking, article.issn):
            if not _normalize_issn(value):
                continue
            search_html = self._get_text(
                LETPUB_SEARCH_URL,
                params={
                    "page": "journalapp",
                    "view": "search",
                    "searchissn": value or "",
                },
            )
            match = _parse_search_result(search_html, value)
            if match:
                journal_id, journal_title = match
                return journal_id, journal_title, f"ISSN {value}"

        if article.journal:
            search_html = self._get_text(
                LETPUB_SEARCH_URL,
                params={
                    "page": "journalapp",
                    "view": "search",
                    "searchname": article.journal,
                },
            )
            match = _parse_search_result(search_html, None)
            if match:
                journal_id, journal_title = match
                if _normalize_title(journal_title) == _normalize_title(
                    article.journal
                ):
                    return journal_id, journal_title, "期刊名称"

        raise LetPubLookupError("LetPub 未找到对应期刊")

    def _lookup_live(self, article: PubMedArticle) -> JournalRanking:
        journal_id, journal_title, matched_by = self._find_journal(article)
        cas_url = LETPUB_CN_DETAIL_URL.format(journal_id=journal_id)
        jcr_url = LETPUB_EN_DETAIL_URL.format(journal_id=journal_id)
        cas_html = self._get_text(cas_url)
        jcr_html = self._get_text(jcr_url)

        (
            cas_edition,
            cas_large_category,
            cas_large_zone,
            cas_categories,
        ) = _parse_cas_partition(cas_html)
        jcr_edition, jcr_categories = _parse_jcr_partition(jcr_html)

        if not cas_large_zone and not jcr_categories:
            raise LetPubLookupError("LetPub 页面没有可解析的分区字段")

        return JournalRanking(
            journal_title=journal_title or article.journal or "期刊名未报告",
            matched_by=matched_by,
            cas_edition=cas_edition,
            cas_large_category=cas_large_category,
            cas_large_zone=cas_large_zone,
            cas_categories=cas_categories,
            cas_source_url=cas_url,
            jcr_edition=jcr_edition,
            jcr_categories=jcr_categories,
            jcr_source_url=jcr_url,
            source_note=(
                "来自 LetPub 公开页面：中科院取2025年3月升级版，"
                "JCR 取 WOS 的 JIF 分区；LetPub 标注 WOS 数据为众包数据，"
                "正式评价或投稿前仍需复核。"
            ),
            data_status="available",
        )

    def _load_cache(self) -> dict[str, dict[str, object]]:
        if not self.cache_path.is_file():
            return {}
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _cached_ranking(
        self,
        cache_key: str,
        *,
        fresh_only: bool,
    ) -> JournalRanking | None:
        with self._cache_lock:
            entry = self._cache.get(cache_key)
        if not isinstance(entry, dict):
            return None
        try:
            saved_at = datetime.fromisoformat(str(entry["saved_at"]))
            ranking = JournalRanking.model_validate(entry["ranking"])
        except (KeyError, TypeError, ValueError):
            return None
        if saved_at.tzinfo is None:
            saved_at = saved_at.replace(tzinfo=timezone.utc)
        is_fresh = datetime.now(timezone.utc) - saved_at <= self.cache_ttl
        if fresh_only and not is_fresh:
            return None
        return ranking.model_copy(
            update={
                "data_status": "cached" if is_fresh else "stale_cache",
                "source_note": (
                    f"{(ranking.source_note or '').rstrip('；。')}；本地缓存时间："
                    f"{saved_at.astimezone(timezone.utc).isoformat()}"
                ).strip("；"),
            }
        )

    def _store_cache(
        self,
        cache_key: str,
        ranking: JournalRanking,
    ) -> None:
        entry = {
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "ranking": ranking.model_dump(mode="json"),
        }
        with self._cache_lock:
            self._cache[cache_key] = entry
            payload = json.dumps(
                self._cache,
                ensure_ascii=False,
                indent=2,
            )
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary_path = self.cache_path.with_suffix(".tmp")
                temporary_path.write_text(payload, encoding="utf-8")
                temporary_path.replace(self.cache_path)
            except OSError:
                return
