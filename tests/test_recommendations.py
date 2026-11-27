"""Deterministic recommendation text.

A recommendation must name an action, not restate the finding: the report is read
by a client who already saw the finding in the register above. These tests pin the
fallback path, because that is what arbitrary LLM-written findings end up in.
"""

from __future__ import annotations

from app.analysis.recommendations import _recommendation_for, _subject
from app.registers.models import FindingV2


def _finding(statement: str, kind: str = "missing_link", priority: str = "P2") -> FindingV2:
    return FindingV2(code="M01", kind=kind, statement=statement, priority=priority, basis_codes=[])


def test_subject_stops_at_the_first_clarification():
    statement = (
        "Продукты P01–P05 не содержат boundary-ограничения (поле boundary = null), "
        "поэтому оффер не имеет зафиксированного дедлайна"
    )
    assert _subject(statement) == "Продукты P01–P05 не содержат boundary-ограничения"


def test_missing_link_fallback_names_an_action():
    statement = (
        "Продукты P01–P05 не содержат boundary-ограничения (поле boundary = null), "
        "поэтому оффер не имеет зафиксированного дедлайна или лимита."
    )
    draft = _recommendation_for(_finding(statement))
    assert draft.change.startswith("Дополнить оффер:")
    assert statement not in draft.change
    assert "Проверить и устранить" not in draft.change
    assert draft.rationale == statement


def test_weak_evidence_fallback_demands_proof_or_removal():
    statement = "E05 утверждает, что 85% студентов находят работу, но источник и период не указаны."
    draft = _recommendation_for(_finding(statement, kind="weak_evidence", priority="P1"))
    assert draft.change.startswith("Подтвердить или снять утверждение:")
    assert "источник, методику и" in draft.change
    assert draft.priority == "P1"


def test_promo_finding_becomes_a_concrete_instruction():
    statement = (
        "Промо-условия из E03 и E06 (скидка до 60% и три курса в подарок) не связаны "
        "ни с одним из продуктов P01–P05."
    )
    draft = _recommendation_for(_finding(statement))
    assert "Привязать акцию к продуктам" in draft.change
    assert "до какой даты" in draft.change


def test_installment_finding_becomes_a_price_reconciliation():
    statement = (
        "Цена продукта P01 (14 303 ₽) не связана с помесячными рассрочками E07–E12; "
        "итоговые суммы рассрочек не совпадают с ценой."
    )
    draft = _recommendation_for(_finding(statement, priority="P1"))
    assert "Сверить цену и рассрочку" in draft.change
    assert draft.priority == "P1"


def test_boundaries_finding_keeps_the_established_advice():
    statement = "Не сформулированы границы результата: что услуга не гарантирует."
    draft = _recommendation_for(_finding(statement))
    assert "блок границ" in draft.change


def test_unknown_kind_still_produces_an_action():
    statement = "Не описан процесс передачи работы после оплаты, клиент не понимает сроки."
    draft = _recommendation_for(_finding(statement, kind="unknown_kind"))
    assert draft.change.startswith("Сформулировать и зафиксировать:")
    assert statement not in draft.change
