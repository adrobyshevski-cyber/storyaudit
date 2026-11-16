"""Evidence selection: ranking, de-duplication and the statement budget.

The register is the report's proof layer, so two failure modes matter: keeping
slogans instead of case studies (the old marker-only score did that) and dropping
legitimate claims to save a slot. These tests pin both the ranking and the caps.
"""

import uuid

from app.analysis.evidence import (
    MIN_CLAIM_SCORE,
    _score,
    _tokens,
    per_source_limit,
    select_claims,
)
from app.config import Settings
from app.models import Proposition


def _proposition(text: str) -> Proposition:
    return Proposition(id=uuid.uuid4(), block_id=uuid.uuid4(), ordinal=0, text=text)


def test_case_study_outranks_slogans_and_boilerplate_is_negative():
    case = "$400,000 экономии в год: результат модернизации ИТ-систем крупного фитнес-бренда"
    credentials = "Мы строим партнерские отношения с крупными брендами и ежегодно входим в мировые рейтинги"
    slogan = "Наш процесс разработки прозрачен для клиента"
    vacancy = "Не нашли подходящую вакансию? Напишите нам."
    legal = "© 2004-2026 ISsoft Solutions. All Right Reserved."

    assert _score(case) > _score(credentials) > _score(slogan) > 0
    assert _score(vacancy) < MIN_CLAIM_SCORE
    assert _score(legal) < MIN_CLAIM_SCORE


def test_abbreviations_normalise_for_duplicate_detection():
    assert _tokens("AI и машинное обучение") == _tokens("Искусственный интеллект и машинное обучение")


def test_selection_keeps_proof_and_drops_boilerplate_and_duplicates():
    claims = select_claims(
        [
            _proposition("Не нашли подходящую вакансию? Напишите нам."),
            _proposition("© 2004-2026 ISsoft Solutions. All Right Reserved."),
            _proposition("AI и машинное обучение"),
            _proposition("Искусственный интеллект и машинное обучение"),
            _proposition("$400,000 экономии в год: результат модернизации ИТ-систем"),
            _proposition("Наш процесс разработки прозрачен для клиента"),
        ],
        limit=5,
        source_code="S01",
    )
    statements = [claim.statement for claim in claims]
    assert any("400,000" in item for item in statements)
    assert any("прозрачен" in item for item in statements)
    assert not any("ваканси" in item.lower() for item in statements)
    assert not any("all right" in item.lower() for item in statements)
    # One meaning, one slot: the AI/ML pair must collapse to a single claim.
    assert sum("обучение" in item.lower() or "интеллект" in item.lower() for item in statements) == 1


def test_per_source_budget_follows_the_role():
    settings = Settings(
        report_max_statements_per_source=12,
        report_max_statements_per_competitor=6,
    )
    assert per_source_limit(settings, role="client") == 12
    assert per_source_limit(settings, role="competitor") == 6
    assert Settings().report_max_statements == 40


def test_report_evidence_section_states_the_selection_rule():
    from app.registers.models import EvidenceRecord
    from app.reports.build_v2 import _section_evidence

    record = EvidenceRecord(
        scan_id=uuid.uuid4(),
        code="E01",
        claim="Наш процесс разработки прозрачен для клиента",
        source_codes=["S01"],
        provenance="primary",
        verification="publication_only",
        relevance="medium",
        freshness="current",
        limitations=[],
        allowed_conclusion="Бренд публично заявляет: наш процесс разработки прозрачен",
    )
    section = _section_evidence([record])
    text = " ".join((block.title or "") + " " + (block.text or "") for block in section.blocks)
    assert "Как отобран реестр" in text
    assert "12" in text and "40" in text
