"""Deterministic anonymisation for interview material.

Policy: raw transcripts are never persisted. The analyst pastes text, this module
redacts it, and only the redacted segment is stored. There is no "undo" table with
the original, by design.

Detection is deliberately conservative — it finds things that are unambiguously
identifying (contacts, links, handles, long numbers, explicit names/companies the
analyst provides) and refuses to guess about the rest. Names that appear without a
context marker cannot be found reliably by regex, so the analyst is expected to pass
them in ``extra_terms``; ``assert_clean`` re-checks the output and the service stores
nothing unless it is clean.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
URL = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
HANDLE = re.compile(r"(?<![\w/])@[A-Za-z0-9_.]{2,}")
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
# Tried in order: international, Russian 8-xxx, bare 10 digits, then a long run.
PHONE_PATTERNS = (
    re.compile(
        r"(?<!\d)\+\d{1,3}[\s\-.]?\(?\d{2,4}\)?[\s\-.]?\d{3}[\s\-.]?\d{2}[\s\-.]?\d{2}(?!\d)"
    ),
    re.compile(
        r"(?<!\d)8[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{2}[\s\-.]?\d{2}(?!\d)"
    ),
    re.compile(
        r"(?<!\d)\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{2}[\s\-.]?\d{2}(?!\d)"
    ),
    re.compile(r"(?<!\d)\+?\d{10,15}(?!\d)"),
)
LONG_NUMBER = re.compile(r"(?<!\d)\d{6,}(?!\d)")
NAME_CONTEXT = re.compile(
    r"(?i)\b(?:меня зовут|my name is|это)\s+([А-ЯЁA-Z][а-яёa-z\-]+(?:\s+[А-ЯЁA-Z][а-яёa-z\-]+)?)"
)
PATRONYMIC = re.compile(r"\b[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+вич\b|\b[А-ЯЁ][а-яё]+вна\b")

CATEGORY_LABELS = {
    "email": "[email]",
    "url": "[ссылка]",
    "handle": "[профиль]",
    "phone": "[телефон]",
    "number": "[номер]",
    "name": "[имя]",
    "organization": "[организация]",
}

# Things that must never survive anonymisation.
FORBIDDEN_AFTER_REDACTION = (EMAIL, URL, *PHONE_PATTERNS)


@dataclass
class AnonymizationResult:
    text: str
    findings: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def total_redactions(self) -> int:
        return sum(self.findings.values())

    def as_dict(self) -> dict[str, object]:
        return {
            "text": self.text,
            "findings": self.findings,
            "total_redactions": self.total_redactions,
            "warnings": self.warnings,
        }


def _redact(text: str, pattern: re.Pattern, category: str, findings: dict[str, int], replacement: str | None = None) -> str:
    def substitute(match: re.Match) -> str:
        findings[category] = findings.get(category, 0) + 1
        return replacement or CATEGORY_LABELS[category]

    return pattern.sub(substitute, text)


def _redact_terms(text: str, terms: dict[str, str], findings: dict[str, int]) -> str:
    # Longest first, so "ООО Ромашка" wins over "Ромашка".
    for term in sorted((t for t in terms if t.strip()), key=len, reverse=True):
        category = terms[term]
        pattern = re.compile(rf"(?<![\w]){re.escape(term)}(?![\w])", re.IGNORECASE)

        def substitute(match: re.Match, category: str = category) -> str:
            findings[category] = findings.get(category, 0) + 1
            return CATEGORY_LABELS[category]

        text = pattern.sub(substitute, text)
    return text


def anonymize(text: str, *, extra_terms: dict[str, str] | None = None) -> AnonymizationResult:
    """Redact identifying material.

    ``extra_terms`` maps a literal term to a category (``name`` / ``organization``),
    e.g. ``{"Анна Петрова": "name", "ООО Ромашка": "organization"}``.
    """
    findings: dict[str, int] = {}
    warnings: list[str] = []
    result = text or ""

    result = _redact(result, EMAIL, "email", findings)
    result = _redact(result, URL, "url", findings)
    result = _redact(result, HANDLE, "handle", findings)
    result = _redact(result, CARD, "number", findings)
    for pattern in PHONE_PATTERNS:
        result = _redact(result, pattern, "phone", findings)
    result = _redact(result, PATRONYMIC, "name", findings)
    result = _redact(result, NAME_CONTEXT, "name", findings)
    result = _redact(result, LONG_NUMBER, "number", findings)
    result = _redact_terms(result, extra_terms or {}, findings)

    if re.search(r"\b(?:ООО|ИП|АО|ЗАО|LLC|Ltd|Inc)\b", result):
        warnings.append("В тексте остались организационно-правовые формы — проверьте названия вручную.")

    return AnonymizationResult(text=result, findings=findings, warnings=warnings)


def assert_clean(text: str) -> None:
    """Raise ``ValueError`` if anything identifying survived redaction."""
    if EMAIL.search(text):
        raise ValueError("В тексте остался адрес электронной почты")
    if URL.search(text):
        raise ValueError("В тексте осталась ссылка")
    if any(pattern.search(text) for pattern in PHONE_PATTERNS):
        raise ValueError("В тексте остался номер телефона")
