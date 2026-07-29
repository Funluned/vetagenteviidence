from __future__ import annotations

import re

from vetevidence.models import EvidenceRecord, PubMedArticle


PATHOGENS = (
    "Streptococcus agalactiae",
    "Giardia duodenalis",
    "Escherichia coli",
    "Staphylococcus aureus",
)
DRUGS = ("quercetin", "luteolin", "amoxicillin", "curcumin")
MECHANISMS = (
    ("nf-κb", "NF-κB"),
    ("nf-kb", "NF-κB"),
    ("nlrp3", "NLRP3"),
    ("ferroptosis", "铁死亡"),
    ("oxidative stress", "氧化应激"),
    ("tight junction", "紧密连接"),
)


def _sentences(text: str) -> list[str]:
    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", text.strip())
        if sentence.strip()
    ]


def _canonical_term(text: str, terms: tuple[str, ...]) -> str | None:
    lowered = text.casefold()
    for term in terms:
        if term.casefold() in lowered:
            return term
    return None


def _extract_species(text: str) -> str | None:
    lowered = text.casefold()
    if re.search(r"\b(mouse|mice|murine)\b", lowered):
        return "小鼠"
    if re.search(r"\b(bovine|cow|cows|cattle)\b", lowered):
        return "牛"
    if re.search(r"\b(gerbil|gerbils)\b", lowered):
        return "蒙古沙鼠"
    return None


def _extract_condition(text: str) -> str | None:
    lowered = text.casefold()
    if "mastitis" in lowered:
        return "乳腺炎"
    if "mammary gland injury" in lowered:
        return "乳腺损伤"
    if "infection" in lowered:
        return "感染"
    return None


def _extract_sample_size(text: str) -> int | None:
    match = re.search(
        r"\b(?:mice|rats|animals?)\s*\(\s*n\s*=\s*(\d+)\s*\)",
        text,
        flags=re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def _extract_dose(text: str) -> str | None:
    match = re.search(
        r"\b(\d+(?:\.\d+)?(?:\s*,\s*\d+(?:\.\d+)?)*)\s*"
        r"(mg/kg|µg/kg|μg/kg|g/kg|mg/L|µg/mL|μg/mL)\b",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    values = re.sub(r"\s*,\s*", ", ", match.group(1))
    return f"{values} {match.group(2)}"


def _extract_route(text: str) -> str | None:
    routes = (
        ("intraperitoneally", "腹腔注射"),
        ("intravenously", "静脉注射"),
        ("orally", "口服"),
        ("subcutaneously", "皮下注射"),
        ("mammary duct injection", "乳腺导管注射"),
    )
    lowered = text.casefold()
    for source, normalized in routes:
        if source in lowered:
            return normalized
    return None


def _extract_duration(text: str) -> str | None:
    match = re.search(
        r"\b\d+(?:\.\d+)?\s*(?:h|hours?|days?|weeks?)\s+"
        r"(?:before|after|for)\b[^.;]{0,80}",
        text,
        flags=re.IGNORECASE,
    )
    return " ".join(match.group(0).split()) if match else None


def _first_sentence_with(sentences: list[str], keyword: str) -> str | None:
    lowered_keyword = keyword.casefold()
    return next(
        (
            sentence
            for sentence in sentences
            if lowered_keyword in sentence.casefold()
        ),
        None,
    )


def _extract_outcomes(sentences: list[str]) -> list[str]:
    outcome_markers = (
        "reduced",
        "inhibited",
        "upregulated",
        "diminished",
        "elevating",
        "demonstrated",
        "revealed",
    )
    outcomes = [
        sentence
        for sentence in sentences
        if any(marker in sentence.casefold() for marker in outcome_markers)
    ]
    return outcomes[:5]


def _extract_key_result(sentences: list[str]) -> str | None:
    conclusion_markers = ("findings indicated", "concluded", "conclusion")
    for sentence in reversed(sentences):
        if any(marker in sentence.casefold() for marker in conclusion_markers):
            return sentence
    return sentences[-1] if sentences else None


def extract_evidence(article: PubMedArticle) -> EvidenceRecord:
    """Extract only explicitly present abstract information using transparent rules."""
    abstract = article.abstract or ""
    combined_text = " ".join((article.title, abstract))
    sentences = _sentences(abstract)

    pathogen = _canonical_term(combined_text, PATHOGENS)
    condition = _extract_condition(combined_text)
    species = _extract_species(combined_text)
    drug = _canonical_term(combined_text, DRUGS)

    if pathogen and species and condition:
        model = f"{pathogen} 诱导的{species}{condition}模型"
    elif species and condition:
        model = f"{species}{condition}模型"
    else:
        model = None

    mechanisms: list[str] = []
    lowered = combined_text.casefold()
    for source, normalized in MECHANISMS:
        if source in lowered and normalized not in mechanisms:
            mechanisms.append(normalized)

    key_result = _extract_key_result(sentences)
    ranking = article.journal_ranking
    limitations = ["仅依据 PubMed 元数据和摘要自动提取，尚未核对论文全文。"]
    if not article.abstract:
        limitations.append("PubMed 未提供摘要，无法提取实验细节。")

    return EvidenceRecord(
        pathogen=pathogen,
        disease_or_condition=condition,
        species=species,
        model=model,
        sample_size=_extract_sample_size(abstract),
        intervention=drug.title() if drug else None,
        drug=drug.title() if drug else None,
        dose=_extract_dose(abstract),
        route=_extract_route(abstract),
        duration=_extract_duration(abstract),
        control=_first_sentence_with(sentences, "control"),
        outcomes=_extract_outcomes(sentences),
        mechanism=mechanisms,
        key_result=key_result,
        limitations=limitations,
        journal=article.journal,
        issn=article.issn or article.issn_linking,
        cas_partition_edition=ranking.cas_edition if ranking else None,
        cas_partition=ranking.cas_display() if ranking else None,
        jcr_partition_edition=ranking.jcr_edition if ranking else None,
        jcr_partition=ranking.jcr_display() if ranking else None,
        journal_ranking_note=ranking.source_note if ranking else None,
        pmid=article.pmid,
        doi=article.doi,
        source_quote=key_result,
        source_url=article.source_url,
    )
