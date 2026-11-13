from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Annotation, Metric, Source
from app.schemas import A6Result
from app.utils import decimal6, normalize_label, stable_hash

FEATURES = ("customer", "problem", "object", "promise", "coherence", "depth", "before", "after")
METRIC_FEATURES = {
    "narrative_clarity": ("customer", "problem", "object", "promise"),
    "narrative_coherence": ("coherence",),
    "narrative_depth": ("depth",),
    "transformation_completeness": ("before", "after"),
}
THRESHOLDS = {
    "narrative_clarity": (Decimal("40"), Decimal("60"), "LOW_CLARITY", "Недостаточная ясность нарратива"),
    "narrative_coherence": (Decimal("40"), Decimal("70"), "LOW_COHERENCE", "Недостаточная согласованность нарратива"),
    "narrative_depth": (Decimal("25"), Decimal("50"), "LOW_DEPTH", "Недостаточная глубина нарратива"),
    "transformation_completeness": (Decimal("40"), Decimal("60"), "LOW_TRANSFORMATION", "Неполная трансформация"),
}
RECOMMENDATIONS = {
    "narrative_clarity": (
        "Явно сформулировать customer, problem, object и promise в связанных утверждениях.",
        "Повысить narrative_clarity.",
    ),
    "narrative_coherence": (
        "Устранить противоречия hero и brand_role между evidence propositions.",
        "Повысить narrative_coherence.",
    ),
    "narrative_depth": (
        "Добавить подтверждённые higher-order meanings и связать их с promise.",
        "Повысить narrative_depth.",
    ),
    "transformation_completeness": (
        "Явно описать подтверждённые состояния Before и After.",
        "Повысить transformation_completeness.",
    ),
}


def weighted_labels(items: list[dict[str, Any]]) -> dict[str, Any]:
    weights: dict[str, Decimal] = defaultdict(Decimal)
    displays: dict[str, str] = {}
    for item in items:
        label = normalize_label(item.get("label", ""))
        confidence = item.get("confidence")
        if not label or label == "unknown" or confidence is None or not 0 <= float(confidence) <= 1:
            continue
        if float(confidence) < 0.20:
            continue
        weights[label] += Decimal(str(confidence))
        displays.setdefault(label, " ".join(str(item["label"]).split()))
    if not weights:
        return {"label": "unknown", "weight": 0.0}
    winner = min(weights, key=lambda label: (-weights[label], label))
    return {"label": winner, "display_label": displays[winner], "weight": float(decimal6(weights[winner]))}


def top_labels(items: list[dict[str, Any]], limit: int = 3) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        grouped[normalize_label(item.get("label", ""))].append(item)
    rows = [weighted_labels(group) for group in grouped.values()]
    rows = [row for row in rows if row["label"] != "unknown"]
    return sorted(rows, key=lambda row: (-row["weight"], row["label"]))[:limit]


def build_profile(annotations: list[Annotation]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    meanings: list[dict[str, Any]] = []
    for annotation in annotations:
        payload = annotation.payload
        if annotation.agent == "A2":
            for key in ("customer", "problem", "object", "promise"):
                grouped[key].append(payload[key])
        elif annotation.agent == "A3":
            grouped["hero"].append(payload["hero"])
            grouped["brand_role"].append(payload["brand_role"])
        elif annotation.agent == "A4":
            meanings.extend(payload["higher_order_meanings"])
            grouped["before"].append(payload["before"])
            grouped["after"].append(payload["after"])
    result: dict[str, Any] = {key: weighted_labels(grouped[key]) for key in ("customer", "problem", "object", "promise", "hero", "brand_role", "before", "after")}
    result["higher_order_meanings"] = top_labels(meanings)
    result["input_hash"] = stable_hash(sorted(str(a.id) for a in annotations))
    return result


def source_strengths(session: Session, scan_id: Any) -> tuple[dict[str, Decimal], dict[str, Any], dict[str, list[dict[str, str]]]]:
    sources = list(session.scalars(select(Source).where(Source.scan_id == scan_id).order_by(Source.id)))
    if not sources:
        raise ValueError("Cannot calculate an empty scan")
    totals = {feature: Decimal(0) for feature in FEATURES}
    breakdown: dict[str, Any] = {"sources": []}
    evidence: dict[str, list[dict[str, str]]] = defaultdict(list)
    for source in sources:
        annotation = session.scalar(
            select(Annotation).where(
                Annotation.scan_id == scan_id,
                Annotation.agent == "A6",
                Annotation.object_type == "source",
                Annotation.object_id == source.id,
                Annotation.is_current.is_(True),
            )
        )
        if annotation is None:
            raise ValueError(f"Missing current A6 annotation for source {source.id}")
        result = A6Result.model_validate(annotation.payload)
        source_row: dict[str, Any] = {"source_id": str(source.id), "features": {}}
        for feature in FEATURES:
            value = result.features[feature]
            totals[feature] += Decimal(str(value.strength))
            source_row["features"][feature] = value.strength
            for prop_id in value.evidence_proposition_ids:
                evidence[feature].append({"source_id": str(source.id), "proposition_id": str(prop_id), "strength": str(value.strength)})
        breakdown["sources"].append(source_row)
    means = {feature: totals[feature] / Decimal(len(sources)) for feature in FEATURES}
    return means, breakdown, evidence


def calculate_values(means: dict[str, Decimal]) -> dict[str, Decimal]:
    clarity = Decimal(100) * (
        Decimal("0.25") * means["customer"]
        + Decimal("0.25") * means["problem"]
        + Decimal("0.20") * means["object"]
        + Decimal("0.30") * means["promise"]
    )
    coherence = Decimal(100) * means["coherence"]
    depth = Decimal(100) * means["depth"]
    transformation = Decimal(100) * (Decimal("0.5") * means["before"] + Decimal("0.5") * means["after"])
    meaning = Decimal("0.5") * clarity + Decimal("0.5") * depth
    change = Decimal("0.5") * coherence + Decimal("0.5") * transformation
    return {
        "narrative_clarity": decimal6(clarity),
        "narrative_coherence": decimal6(coherence),
        "narrative_depth": decimal6(depth),
        "transformation_completeness": decimal6(transformation),
        "meaning_capital": decimal6(meaning),
        "change_capital": decimal6(change),
        "storyaudit_index": decimal6(Decimal("0.6") * meaning + Decimal("0.4") * change),
    }


def finding_for_metric(metric: Metric, evidence: dict[str, list[dict[str, str]]]) -> dict[str, Any] | None:
    high, medium, code, title = THRESHOLDS[metric.code]
    if metric.value >= medium:
        return None
    severity = "high" if metric.value < high else "medium"
    ids: list[str] = []
    rows: list[dict[str, str]] = []
    for feature in METRIC_FEATURES[metric.code]:
        rows.extend(evidence.get(feature, []))
    rows.sort(key=lambda row: (Decimal(row["strength"]), row["source_id"], row["proposition_id"]))
    for row in rows:
        if row["proposition_id"] not in ids:
            ids.append(row["proposition_id"])
        if len(ids) == 3:
            break
    if not ids:
        return None
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "explanation": f"Значение метрики {metric.value} ниже порога {medium}.",
        "evidence_proposition_ids": ids,
    }
