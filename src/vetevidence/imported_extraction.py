"""Transparent field extraction for user-imported titles and abstracts."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ImportedExperimentalFields:
    pathogen: str | None
    disease_or_condition: str | None
    species: str | None
    model: str | None
    sample_size: int | None
    intervention: str | None
    dose: str | None
    route: str | None
    duration: str | None
    control: str | None
    outcomes: list[str]
    mechanism: list[str]
    key_result: str | None
    source_quote: str | None


_PATHOGENS = (
    "Streptococcus agalactiae",
    "Giardia duodenalis",
    "Escherichia coli",
    "Staphylococcus aureus",
)
_DRUGS = ("quercetin", "luteolin", "amoxicillin", "curcumin")
_MECHANISMS = (
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


def _term(text: str, values: tuple[str, ...]) -> str | None:
    normalized = text.casefold()
    return next(
        (value for value in values if value.casefold() in normalized),
        None,
    )


def _species(text: str) -> str | None:
    normalized = text.casefold()
    if re.search(r"\b(mouse|mice|murine)\b", normalized):
        return "小鼠"
    if re.search(r"\b(bovine|cow|cows|cattle)\b", normalized):
        return "牛"
    if re.search(r"\b(gerbil|gerbils)\b", normalized):
        return "蒙古沙鼠"
    return None


def _condition(text: str) -> str | None:
    normalized = text.casefold()
    if "mastitis" in normalized:
        return "乳腺炎"
    if "mammary gland injury" in normalized:
        return "乳腺损伤"
    if "infection" in normalized:
        return "感染"
    return None


def _match(text: str, pattern: str) -> str | None:
    matched = re.search(pattern, text, flags=re.IGNORECASE)
    return " ".join(matched.group(0).split()) if matched else None


def _sample_size(text: str) -> int | None:
    matched = re.search(
        r"\b(?:mice|rats|animals?)\s*\(\s*n\s*=\s*(\d+)\s*\)",
        text,
        flags=re.IGNORECASE,
    )
    return int(matched.group(1)) if matched else None


def _dose(text: str) -> str | None:
    matched = re.search(
        r"\b(\d+(?:\.\d+)?(?:\s*,\s*\d+(?:\.\d+)?)*)\s*"
        r"(mg/kg|μg/kg|ug/kg|g/kg|mg/L|μg/mL|ug/mL)\b",
        text,
        flags=re.IGNORECASE,
    )
    if not matched:
        return None
    values = re.sub(r"\s*,\s*", ", ", matched.group(1))
    return f"{values} {matched.group(2)}"


def _route(text: str) -> str | None:
    normalized = text.casefold()
    mappings = (
        ("intraperitoneally", "腹腔注射"),
        ("intravenously", "静脉注射"),
        ("orally", "口服"),
        ("subcutaneously", "皮下注射"),
        ("mammary duct injection", "乳腺导管注射"),
    )
    return next(
        (target for source, target in mappings if source in normalized),
        None,
    )


def extract_imported_experimental_fields(
    title: str,
    abstract: str | None,
) -> ImportedExperimentalFields:
    abstract_text = abstract or ""
    combined = " ".join((title, abstract_text))
    sentences = _sentences(abstract_text)
    pathogen = _term(combined, _PATHOGENS)
    species = _species(combined)
    condition = _condition(combined)
    drug = _term(combined, _DRUGS)
    mechanisms = []
    normalized = combined.casefold()
    for source, target in _MECHANISMS:
        if source in normalized and target not in mechanisms:
            mechanisms.append(target)
    outcomes = [
        sentence
        for sentence in sentences
        if any(
            marker in sentence.casefold()
            for marker in (
                "reduced",
                "inhibited",
                "increased",
                "upregulated",
                "diminished",
                "demonstrated",
                "revealed",
                "synerg",
            )
        )
    ][:5]
    key_result = next(
        (
            sentence
            for sentence in reversed(sentences)
            if any(
                marker in sentence.casefold()
                for marker in ("findings indicated", "concluded", "conclusion")
            )
        ),
        sentences[-1] if sentences else None,
    )
    control = next(
        (sentence for sentence in sentences if "control" in sentence.casefold()),
        None,
    )
    model = None
    if pathogen and species and condition:
        model = f"{pathogen} 诱导的{species}{condition}模型"
    elif species and condition:
        model = f"{species}{condition}模型"
    return ImportedExperimentalFields(
        pathogen=pathogen,
        disease_or_condition=condition,
        species=species,
        model=model,
        sample_size=_sample_size(abstract_text),
        intervention=drug.title() if drug else None,
        dose=_dose(abstract_text),
        route=_route(abstract_text),
        duration=_match(
            abstract_text,
            r"\b\d+(?:\.\d+)?\s*(?:h|hours?|days?|weeks?)\s+"
            r"(?:before|after|for)\b[^.;]{0,80}",
        ),
        control=control,
        outcomes=outcomes,
        mechanism=mechanisms,
        key_result=key_result,
        source_quote=key_result,
    )
