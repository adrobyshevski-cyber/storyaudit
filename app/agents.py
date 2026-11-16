import logging
import uuid
from typing import Any, Callable

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from app import llm_cache
from app.analysis.common import propositions_by_source, registry_map
from app.analysis.actants import UNAVAILABLE, build_actant_draft
from app.analysis.consistency import build_consistency_draft, persist_consistency
from app.analysis.critic import apply_critic_draft, build_critic_draft
from app.analysis.findings import draft_findings, persist_findings
from app.analysis.journey import build_journey_draft, persist_journey
from app.analysis.landscape import (
    LandscapeDraft,
    build_landscape_draft,
    persist_landscape,
)
from app.analysis.narrative import (
    allowed_refs,
    build_bundle,
    build_narrative_draft,
    numeric_corpus,
    persist_narrative,
    validate_narrative,
)
from app.analysis.recommendations import build_plan, persist_plan, plan_from_payload
from app.analysis.scorecard import ScoreDraft, build_scorecard_draft, persist_scorecard
from app.registers.models import (
    ActantMap,
    ActantRole,
    ConsistencyCell,
    EvidenceRecord,
    FindingV2,
    JourneyStep,
    LayerClaim,
    LandscapeEntry,
    Product,
    RecommendationV2,
    ScorecardEntry,
)
from app.analysis.evidence import ClaimDraft, persist_claims, select_claims
from app.analysis.language import LanguageDraft, extract_language, persist_language
from app.analysis.products import extract_products
from app.analysis.verify import verify_source
from app.calculations import (
    FEATURES,
    RECOMMENDATIONS,
    build_profile,
    calculate_values,
    finding_for_metric,
    source_strengths,
)
from app.llm import LLMClient
from app.llm_usage import DbUsageRecorder
from app.config import get_settings
from app.models import (
    Annotation,
    Block,
    CalculationRun,
    Capital,
    Document,
    Finding,
    FindingEvidence,
    IndexResult,
    Job,
    Metric,
    Proposition,
    Recommendation,
    Scan,
    Source,
    SourceProfile,
    now,
)
from app.scans import add_website_source, new_calculation_run, run_scan
from app.registers.codes import allocate_code
from app.schemas import (
    A1Result,
    A2BatchResult,
    A2Result,
    A3BatchResult,
    A3Result,
    A4BatchResult,
    A4Result,
    A6Result,
    ActantMapResult,
    ConsistencyResult,
    CriticResult,
    EvidenceResult,
    FindingsResult,
    JourneyResult,
    LandscapeResult,
    NarrativeResult,
    PlanResult,
    ScorecardResult,
    WebsiteSourceCreate,
    LanguageResult,
    ProductsResult,
)
from app.utils import stable_hash

logger = logging.getLogger("storyaudit.agents")


def _label(value: str, confidence: float = 0.85) -> dict[str, Any]:
    return {"label": value, "confidence": confidence}


def _unknown() -> dict[str, Any]:
    return {"label": "unknown", "confidence": 0.0}


SINGLE_SCHEMAS = {"A2": A2Result, "A3": A3Result, "A4": A4Result}
BATCH_SCHEMAS = {"A2": A2BatchResult, "A3": A3BatchResult, "A4": A4BatchResult}


def ensure_known_codes(
    label: str, allowed: set[str], items: list[dict[str, Any]], field: str = "source_codes"
) -> None:
    """Reject invented references with a message the model can act on.

    Raised inside the LLM repair loop, so the provider sees the unknown codes and
    the allowed vocabulary instead of failing the whole scan.
    """
    unknown = sorted({code for item in items for code in (item.get(field) or [])} - allowed)
    if unknown:
        raise ValueError(
            f"{label}: unknown {field} {unknown}. Allowed: {sorted(allowed)}"
        )


def ensure_unique(label: str, items: list[dict[str, Any]], fields: tuple[str, ...]) -> None:
    seen = set()
    for item in items:
        key = tuple(item.get(field) for field in fields)
        if key in seen:
            raise ValueError(f"{label}: duplicate entry {key}")
        seen.add(key)


def _validate_a6(payload: dict[str, Any], allowed_ids: set[str]) -> None:
    features = payload["features"]
    missing = [name for name in FEATURES if name not in features]
    if missing:
        raise ValueError(f"A6: missing feature names {missing}; allowed: {list(FEATURES)}")
    foreign = sorted(
        {
            str(item)
            for feature in features.values()
            for item in feature["evidence_proposition_ids"]
        }
        - allowed_ids
    )
    if foreign:
        raise ValueError(
            f"A6: evidence ids not in this source: {foreign[:5]}; copy ids from the input"
        )


def _validate_actants(
    payload: dict[str, Any], allowed_sources: set[str], allowed_evidence: set[str]
) -> None:
    roles = payload["roles"]
    ensure_unique("ACTANT", roles, ("role", "object_level"))
    ensure_known_codes("ACTANT", allowed_sources, roles)
    ensure_known_codes("ACTANT", allowed_evidence, roles, field="evidence_codes")
    missing = [item["role"] for item in roles if item["status"] == "F" and not item["source_codes"]]
    if missing:
        raise ValueError(
            f"ACTANT: roles marked F need a source code: {missing}. "
            f"Use one of {sorted(allowed_sources)}"
        )


def _validate_findings(payload: dict[str, Any], allowed: set[str]) -> None:
    findings = payload["findings"]
    ensure_known_codes("FINDINGS", allowed, findings, field="basis_codes")
    empty = [item["statement"][:60] for item in findings if not item["basis_codes"]]
    if empty:
        raise ValueError(
            f"FINDINGS: every finding needs a basis code from {sorted(allowed)}: {empty}"
        )
    weak_gaps = [
        item["statement"][:60]
        for item in findings
        if item["kind"] == "gap" and len(set(item["basis_codes"])) < 2
    ]
    if weak_gaps:
        raise ValueError(
            "FINDINGS: a gap needs two distinct observable sides, otherwise it is a W: "
            f"{weak_gaps}"
        )


def _validate_scorecard(
    payload: dict[str, Any], audit_mode: str, expected: set[tuple[str, str]]
) -> None:
    items = payload["items"]
    answered = {(item["dimension"], item["criterion"]) for item in items}
    missing = sorted(expected - answered)
    if missing:
        raise ValueError(
            f"SCORECARD: every criterion must be present, missing {missing[:10]}. "
            "Score it or set score=null with a reason."
        )
    unknown = sorted(answered - expected)
    if unknown:
        raise ValueError(f"SCORECARD: criteria not in the rubric: {unknown[:10]}")
    unexplained = [
        f"{item['dimension']}/{item['criterion']}"
        for item in items
        if item["score"] is None and not (item["na_reason"] or "").strip()
    ]
    if unexplained:
        raise ValueError(f"SCORECARD: N/A needs a reason: {unexplained}")
    if audit_mode == "desk":
        scored = [
            f"{item['dimension']}/{item['criterion']}"
            for item in items
            if item["dimension"] == "experience" and item["score"] is not None
        ]
        if scored:
            raise ValueError(
                "SCORECARD: a desk audit cannot score the experience dimension, "
                f"use score=null with a reason: {scored}"
            )


def _annotation_mock(agent: str, proposition: Proposition) -> dict[str, Any]:
    """Deterministic mock payloads used by LLM_PROVIDER=mock."""
    if agent == "A2":
        return {
            "customer": _label("customer"),
            "problem": _label(proposition.text[:120]),
            "object": _label("product"),
            "promise": _label(proposition.text[:120]),
        }
    if agent == "A3":
        return {
            "hero": _label("customer"),
            "brand_role": _label("guide"),
            "conflict": {"present": False, "confidence": 0.9, "description": ""},
        }
    return {
        "higher_order_meanings": [_label("clarity", 0.7)],
        "before": _label("uncertainty", 0.7),
        "after": _label("confidence", 0.8),
    }


def _annotation_targets(session: Session, job: Job) -> list[Proposition]:
    """Propositions covered by a job: a whole chunk or a single reanalysis target."""
    ids = job.input_snapshot.get("proposition_ids")
    if not ids:
        proposition = session.get(Proposition, job.object_id)
        if not proposition:
            raise ValueError("Proposition not found")
        return [proposition]
    ordered = [uuid.UUID(str(item)) for item in ids]
    found = {
        item.id: item
        for item in session.scalars(select(Proposition).where(Proposition.id.in_(ordered)))
    }
    missing = [str(item) for item in ordered if item not in found]
    if missing:
        raise ValueError(f"Propositions not found: {missing}")
    return [found[item] for item in ordered]


def _check_batch_answer(requested: list[str], result: dict[str, Any]) -> None:
    """A batch answer must cover exactly the requested propositions, once each."""
    answered = [str(item["proposition_id"]) for item in result["items"]]
    if len(set(answered)) != len(answered):
        raise ValueError("batch answer repeats a proposition_id")
    missing = sorted(set(requested) - set(answered))
    foreign = sorted(set(answered) - set(requested))
    if missing or foreign:
        raise ValueError(
            f"batch answer covers the wrong propositions: "
            f"missing={missing[:5]}, foreign={foreign[:5]}"
        )


def canonical_proposition_text(text: str) -> str:
    """Normalisation used to reuse one answer for repeated corpus text.

    Menus, footers and repeated offers produce identical propositions on several
    pages. Every proposition still needs its own annotation (registers read one
    annotation per proposition), but they do not need several provider calls.
    """
    return " ".join(text.split()).strip().casefold()


def _validated(
    agent: str,
    schema: type[BaseModel],
    payload: Any,
    validate: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any]:
    """Apply the same schema and traceability checks to drafts and cached answers."""
    try:
        result = schema.model_validate(payload).model_dump(mode="json")
    except ValidationError as exc:
        raise ValueError(f"Invalid {agent} payload: {exc}") from exc
    if validate is not None:
        validate(result)
    return result


def run_stage(
    llm: LLMClient,
    job: Job,
    *,
    agent: str,
    schema: type[BaseModel],
    context: dict[str, Any],
    mock: dict[str, Any],
    validate: Callable[[dict[str, Any]], None] | None = None,
    cache: bool = True,
) -> tuple[dict[str, Any], str]:
    """Run a model stage, or return its deterministic draft.

    Returns ``(payload, source)`` where ``source`` is ``"llm"``, ``"cache"`` or
    ``"draft"``. Drafts and cached answers go through the same schema and the same
    ``validate`` callback (code allow-lists, unique keys) as a fresh answer, so
    ``ANALYSIS_MODE`` changes the wording, never the report's invariants.

    Reanalysis always calls the provider: an analyst asking for a second opinion
    must not be handed a cached or template answer.
    """
    settings = get_settings()
    # Reanalysis is an explicit request for a second opinion: a real provider must
    # be asked even in hybrid/deterministic mode. With LLM_PROVIDER=mock the draft
    # and the "provider" answer are the same payload, so nothing is lost.
    forced = job.job_type == "reanalysis" and settings.llm_provider != "mock"
    if not forced and not settings.uses_llm(agent):
        payload = _validated(agent, schema, mock, validate)
        logger.info(
            "Stage used the deterministic draft",
            extra={"service": "agents", "agent": agent, "scan_id": str(job.scan_id)},
        )
        return payload, "draft"

    model, _ = settings.model_for_agent(agent)
    use_cache = (
        cache
        and settings.llm_cache_enabled
        and settings.llm_provider != "mock"
        and job.job_type != "reanalysis"
    )
    key = (
        llm_cache.request_key(agent=agent, model=model, schema=schema, context=context)
        if use_cache
        else ""
    )
    if use_cache:
        cached = llm_cache.get(key)
        if cached is not None:
            try:
                payload = _validated(agent, schema, cached, validate)
            except ValueError:
                # A schema or an allow-list changed since the row was written.
                logger.warning(
                    "Cached answer ignored: it no longer validates",
                    extra={"service": "agents", "agent": agent},
                )
            else:
                return payload, "cache"

    payload = llm.generate(
        agent=agent, schema=schema, context=context, mock=mock, validate=validate
    )
    if use_cache:
        llm_cache.put(key, agent=agent, model=model, payload=payload)
    return payload, "llm"


def _text_cache_enabled(settings: Any, job: Job) -> bool:
    return bool(
        settings.llm_cache_enabled
        and settings.llm_provider != "mock"
        and job.job_type != "reanalysis"
    )


def _cached_annotations(session: Session, job: Job) -> dict[str, dict[str, Any]]:
    """Current payloads of this generation, keyed by canonical proposition text."""
    rows = session.execute(
        select(Proposition.text, Annotation.payload)
        .join(Annotation, Annotation.object_id == Proposition.id)
        .where(
            Annotation.scan_id == job.scan_id,
            Annotation.agent == job.agent,
            Annotation.object_type == "proposition",
            Annotation.analysis_generation == job.analysis_generation,
            Annotation.is_current.is_(True),
        )
    ).all()
    return {canonical_proposition_text(text): payload for text, payload in rows}


def _annotate_batch(session: Session, job: Job, llm: LLMClient, targets: list[Proposition]) -> None:
    """One provider call per chunk; repeated texts reuse one answer.

    Annotations are still stored per proposition with the usual payload, so
    ``PROFILE``, metrics and the report read exactly what they read before.
    """
    settings = get_settings()
    groups: dict[str, list[Proposition]] = {}
    for item in targets:
        groups.setdefault(canonical_proposition_text(item.text), []).append(item)

    current = _cached_annotations(session, job) if settings.uses_llm(job.agent) else {}
    reused: dict[str, dict[str, Any]] = {
        canonical: payload for canonical, payload in current.items() if canonical in groups
    }

    use_text_cache = _text_cache_enabled(settings, job)
    model, _ = settings.model_for_agent(job.agent)
    key_map = {
        canonical: llm_cache.text_key(agent=job.agent, model=model, text=canonical)
        for canonical in groups
    }
    if use_text_cache:
        hits = llm_cache.get_many(list(key_map.values()))
        for canonical, cache_key in key_map.items():
            payload = hits.get(cache_key)
            if payload is None or canonical in reused:
                continue
            try:
                reused[canonical] = _validated(job.agent, SINGLE_SCHEMAS[job.agent], payload, None)
            except ValueError:
                continue

    pending = [(canonical, group[0]) for canonical, group in groups.items() if canonical not in reused]
    answers: dict[str, dict[str, Any]] = {}
    source = "cache"
    if pending:
        requested = [str(item.id) for _, item in pending]
        mocks = {str(item.id): _annotation_mock(job.agent, item) for _, item in pending}
        result, source = run_stage(
            llm,
            job,
            agent=job.agent,
            schema=BATCH_SCHEMAS[job.agent],
            context={"propositions": [{"id": str(item.id), "text": item.text} for _, item in pending]},
            mock={"items": [{"proposition_id": pid, **mocks[pid]} for pid in requested]},
            validate=lambda payload: _check_batch_answer(requested, payload),
            cache=False,
        )
        by_id = {str(item.id): canonical for canonical, item in pending}
        for item in result["items"]:
            canonical = by_id[str(item["proposition_id"])]
            answers[canonical] = {key: value for key, value in item.items() if key != "proposition_id"}
        if use_text_cache and source == "llm":
            llm_cache.put_many(
                {key_map[canonical]: payload for canonical, payload in answers.items()},
                agent=job.agent,
                model=model,
            )

    for canonical, group in groups.items():
        payload = answers.get(canonical) or reused.get(canonical)
        if payload is None:
            raise ValueError("Annotation answer is missing for a proposition group")
        item_source = source if canonical in answers else "cache"
        for item in group:
            save_annotation(session, job, "proposition", item.id, dict(payload), source=item_source)


def save_annotation(
    session: Session,
    job: Job,
    object_type: str,
    object_id: uuid.UUID,
    payload: dict[str, Any],
    *,
    source: str = "llm",
) -> Annotation:
    """Persist one annotation version.

    ``source`` records how the payload was produced (``llm``, ``cache``, ``draft``)
    so a templated report can be told apart from a model-written one afterwards.
    """
    scan = session.get(Scan, job.scan_id)
    publish_current = scan.analysis_generation == job.analysis_generation
    key = f"{scan.id}:{job.agent}:{object_type}:{object_id}"
    if publish_current:
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key})
    previous = session.scalar(select(Annotation).where(
        Annotation.scan_id == scan.id,
        Annotation.agent == job.agent,
        Annotation.object_type == object_type,
        Annotation.object_id == object_id,
        Annotation.is_current.is_(True),
    ))
    if previous and publish_current:
        previous.is_current = False
        session.flush()
    deterministic = job.agent in {"A0", "A5", "A7", "A8a", "A8b", "PROFILE"} or source == "draft"
    annotation = Annotation(
        scan_id=scan.id,
        agent=job.agent,
        object_type=object_type,
        object_id=object_id,
        issue_id=job.issue_id,
        analysis_generation=job.analysis_generation,
        scope_hash=job.scope_hash,
        payload=payload,
        methodology_version=scan.methodology_version,
        schema_version=scan.schema_version,
        prompt_version="not_applicable" if deterministic else scan.prompt_version,
        model_version=f"code:{scan.schema_version}" if deterministic else scan.model_version,
        supersedes_annotation_id=previous.id if previous and publish_current else None,
        is_current=publish_current,
    )
    session.add(annotation)
    session.flush()
    return annotation


def _propositions_for_source(session: Session, source_id: uuid.UUID) -> list[Proposition]:
    return list(session.scalars(
        select(Proposition)
        .join(Block, Proposition.block_id == Block.id)
        .join(Document, Block.document_id == Document.id)
        .where(Document.source_id == source_id)
        .order_by(Proposition.id)
    ))


def _source_annotations(session: Session, scan_id: uuid.UUID, source_id: uuid.UUID) -> list[Annotation]:
    proposition_ids = [item.id for item in _propositions_for_source(session, source_id)]
    if not proposition_ids:
        return []
    return list(session.scalars(select(Annotation).where(
        Annotation.scan_id == scan_id,
        Annotation.agent.in_(["A2", "A3", "A4"]),
        Annotation.object_type == "proposition",
        Annotation.object_id.in_(proposition_ids),
        Annotation.is_current.is_(True),
    ).order_by(Annotation.id)))


def execute_job(session: Session, job: Job) -> None:
    scan = session.get(Scan, job.scan_id)
    if not scan or scan.analysis_generation != job.analysis_generation:
        return
    llm = LLMClient(recorder=DbUsageRecorder(
        scan_id=scan.id,
        job_id=job.id,
        stage_run_id=job.stage_run_id,
        agent=job.agent,
    ))

    if job.agent == "INGEST":
        # Public request path: crawl the site inside the worker (the HTTP request
        # must return immediately), then hand the corpus to the regular pipeline.
        # The job can be retried after a lost lease *after* run_scan committed;
        # in that case the scan is no longer draft and ingestion is already done.
        if scan.status != "draft":
            return
        # Errors are marked non-retryable: a site without text will not appear on
        # the second attempt either, and every retry costs a network crawl.
        try:
            add_website_source(
                session,
                scan.id,
                WebsiteSourceCreate(
                    url=str(job.input_snapshot.get("website_url") or ""),
                    max_pages=int(job.input_snapshot.get("max_pages") or 5),
                ),
            )
            run_scan(session, scan.id)
        except Exception as exc:
            exc.retryable = False
            raise
        return

    if job.agent == "A0":
        source_count = session.scalar(select(func.count(Source.id)).where(Source.scan_id == scan.id))
        document_count = session.scalar(select(func.count(Document.id)).join(Source).where(Source.scan_id == scan.id))
        block_count = session.scalar(select(func.count(Block.id)).join(Document).join(Source).where(Source.scan_id == scan.id))
        if job.input_snapshot.get("corpus_hash") != scan.corpus_hash:
            raise ValueError("Corpus hash mismatch")
        save_annotation(session, job, "scan", scan.id, {
            "corpus_hash": scan.corpus_hash,
            "source_count": source_count,
            "document_count": document_count,
            "block_count": block_count,
        })
        return

    if job.agent == "A1":
        document = session.get(Document, job.object_id)
        blocks = list(session.scalars(select(Block).where(Block.document_id == document.id).order_by(Block.ordinal)))
        mock = {"propositions": [{"block_id": str(block.id), "text": block.text[:1000], "confidence": 0.95} for block in blocks]}
        result, source = run_stage(
            llm, job, agent="A1", schema=A1Result,
            context={"blocks": [{"id": str(b.id), "text": b.text} for b in blocks]},
            mock=mock,
        )
        valid_blocks = {str(block.id) for block in blocks}
        seen = set()
        for item in result["propositions"]:
            if item["block_id"] not in valid_blocks:
                raise ValueError("A1 returned a block outside the document")
            normalized = " ".join(item["text"].lower().split())
            identity = (item["block_id"], normalized)
            if identity in seen:
                raise ValueError("A1 returned duplicate propositions")
            seen.add(identity)
            ordinal = session.scalar(select(func.coalesce(func.max(Proposition.ordinal), -1)).where(Proposition.block_id == uuid.UUID(item["block_id"]))) + 1
            session.add(Proposition(block_id=uuid.UUID(item["block_id"]), ordinal=ordinal, text=item["text"], confidence=item["confidence"]))
        save_annotation(session, job, "document", document.id, result)
        return

    if job.agent in {"A2", "A3", "A4"}:
        targets = _annotation_targets(session, job)
        if len(targets) > 1:
            _annotate_batch(session, job, llm, targets)
            return
        proposition = targets[0]
        result, source = run_stage(
            llm, job,
            agent=job.agent,
            schema=SINGLE_SCHEMAS[job.agent],
            context={"proposition": proposition.text},
            mock=_annotation_mock(job.agent, proposition),
        )
        save_annotation(session, job, "proposition", proposition.id, result, source=source)
        return

    if job.agent == "A5":
        annotations = _source_annotations(session, scan.id, job.object_id)
        payload = {
            "source_id": str(job.object_id),
            "annotation_ids": sorted(str(item.id) for item in annotations),
            "proposition_ids": sorted(str(item.id) for item in _propositions_for_source(session, job.object_id)),
            "input_hash": stable_hash(sorted(str(item.id) for item in annotations)),
        }
        save_annotation(session, job, "source", job.object_id, payload)
        return

    if job.agent == "PROFILE":
        annotations = _source_annotations(session, scan.id, job.object_id)
        payload = build_profile(annotations)
        session.execute(update(SourceProfile).where(SourceProfile.scan_id == scan.id, SourceProfile.source_id == job.object_id, SourceProfile.is_current.is_(True)).values(is_current=False, invalidated_at=now()))
        session.add(SourceProfile(
            scan_id=scan.id,
            source_id=job.object_id,
            analysis_generation=job.analysis_generation,
            config_hash=stable_hash(scan.config_snapshot),
            payload=payload,
            is_current=True,
        ))
        save_annotation(session, job, "source", job.object_id, payload)
        return

    if job.agent == "A6":
        propositions = _propositions_for_source(session, job.object_id)
        profile = session.scalar(select(SourceProfile).where(
            SourceProfile.scan_id == scan.id,
            SourceProfile.source_id == job.object_id,
            SourceProfile.is_current.is_(True),
        ))
        if not profile:
            raise ValueError("Current source profile is missing")
        evidence = [str(item.id) for item in propositions[:5]]
        if not propositions:
            features = {name: {"strength": 0.0, "evidence_proposition_ids": []} for name in FEATURES}
        else:
            data = profile.payload
            features = {}
            for name in ("customer", "problem", "object", "promise", "before", "after"):
                known = data[name]["label"] != "unknown"
                features[name] = {"strength": 0.8 if known else 0.0, "evidence_proposition_ids": evidence if known else []}
            conflicts = [a for a in _source_annotations(session, scan.id, job.object_id) if a.agent == "A3" and a.payload["conflict"]["present"]]
            coherence = max(0.0, 1.0 - len(conflicts) / max(1, len(propositions)))
            features["coherence"] = {"strength": coherence, "evidence_proposition_ids": evidence}
            depth = min(1.0, len(data["higher_order_meanings"]) / 3)
            features["depth"] = {"strength": depth, "evidence_proposition_ids": evidence if depth else []}
        if propositions:
            result, source = run_stage(
                llm, job,
                agent="A6",
                schema=A6Result,
                context={
                    "profile": profile.payload,
                    "evidence": [{"id": str(p.id), "text": p.text} for p in propositions],
                    "allowed_feature_names": list(FEATURES),
                    "evidence_id_rule": "evidence_proposition_ids must be copied from the ids above",
                },
                mock={"features": features},
                validate=lambda payload: _validate_a6(payload, {str(p.id) for p in propositions}),
            )
        else:
            result, source = {"features": features}, "draft"
        allowed = set(str(p.id) for p in propositions)
        for feature in result["features"].values():
            if not set(feature["evidence_proposition_ids"]).issubset(allowed):
                raise ValueError("A6 returned foreign evidence")
        save_annotation(session, job, "source", job.object_id, result, source=source)
        return

    if job.agent == "A7":
        means, _, evidence = source_strengths(session, scan.id)
        values = calculate_values(means)
        run = session.scalar(select(CalculationRun).where(CalculationRun.scan_id == scan.id, CalculationRun.analysis_generation == job.analysis_generation))
        if not run:
            run = new_calculation_run(session, scan, job.analysis_generation, stable_hash(values))
        candidates = []
        for code in ("narrative_clarity", "narrative_coherence", "narrative_depth", "transformation_completeness"):
            metric = Metric(scan_id=scan.id, calculation_run_id=run.id, analysis_generation=job.analysis_generation, code=code, value=values[code])
            candidate = finding_for_metric(metric, evidence)
            if candidate:
                candidate["metric_code"] = code
                candidates.append(candidate)
        save_annotation(session, job, "scan", scan.id, {"calculation_run_id": str(run.id), "finding_candidates": candidates})
        return

    if job.agent == "A8a":
        run = session.get(CalculationRun, uuid.UUID(job.input_snapshot["calculation_run_id"]))
        means, breakdown, evidence = source_strengths(session, scan.id)
        values = calculate_values(means)
        for code in ("narrative_clarity", "narrative_coherence", "narrative_depth", "transformation_completeness"):
            metric = Metric(
                scan_id=scan.id, calculation_run_id=run.id, analysis_generation=job.analysis_generation,
                code=code, value=values[code], breakdown=breakdown, config_hash=run.config_hash,
            )
            session.add(metric)
            session.flush()
            candidate = finding_for_metric(metric, evidence)
            if candidate:
                finding = Finding(
                    scan_id=scan.id, metric_id=metric.id, calculation_run_id=run.id,
                    code=candidate["code"], severity=candidate["severity"], title=candidate["title"],
                    explanation=candidate["explanation"],
                )
                session.add(finding)
                session.flush()
                for proposition_id in candidate["evidence_proposition_ids"]:
                    session.add(FindingEvidence(finding_id=finding.id, proposition_id=uuid.UUID(proposition_id)))
        for code in ("meaning_capital", "change_capital"):
            session.add(Capital(scan_id=scan.id, calculation_run_id=run.id, code=code, value=values[code]))
        session.add(IndexResult(scan_id=scan.id, calculation_run_id=run.id, value=values["storyaudit_index"]))
        save_annotation(session, job, "scan", scan.id, {key: str(value) for key, value in values.items()})
        return

    if job.agent == "A8b":
        run = session.get(CalculationRun, uuid.UUID(job.input_snapshot["calculation_run_id"]))
        findings = list(session.scalars(select(Finding).join(Metric, Finding.metric_id == Metric.id).where(Finding.calculation_run_id == run.id)))
        recommendation_ids = []
        for finding in findings:
            metric = session.get(Metric, finding.metric_id)
            action, effect = RECOMMENDATIONS[metric.code]
            recommendation = Recommendation(
                finding_id=finding.id,
                title=f"Рекомендация: {finding.title}"[:200],
                action=action,
                expected_effect=effect,
                priority=1 if finding.severity == "high" else 3,
            )
            session.add(recommendation)
            session.flush()
            recommendation_ids.append(str(recommendation.id))
        for model in (Metric, Capital, IndexResult, Finding):
            session.execute(update(model).where(model.scan_id == scan.id, model.is_current.is_(True)).values(is_current=False))
        for model in (Metric, Capital, IndexResult, Finding):
            if model is Finding:
                session.execute(update(model).where(model.calculation_run_id == run.id).values(is_current=True))
            else:
                session.execute(update(model).where(model.calculation_run_id == run.id).values(is_current=True))
        run.status = "succeeded"
        run.completed_at = now()
        scan.current_calculation_run_id = run.id
        scan.error_summary = None
        # The scan becomes `succeeded` only when every initial job of the
        # generation is done (see app.pipeline._finalize_if_ready), so the
        # report never observes a half-built register.
        save_annotation(session, job, "scan", scan.id, {"recommendation_ids": recommendation_ids})
        return

    if job.agent == "VERIFY":
        entry = registry_map(session, scan).get(job.object_id)
        if entry is None:
            raise ValueError("Source is not registered")
        check = verify_source(session, entry)
        save_annotation(session, job, "source", job.object_id, {
            "skipped": check.skipped,
            "status_code": check.status_code,
            "final_url": check.final_url,
            "error": check.error,
            "notes": check.notes,
        })
        return

    if job.agent == "PRODUCT":
        if session.scalar(select(Product.id).where(Product.scan_id == scan.id).limit(1)):
            save_annotation(session, job, "scan", scan.id, {"skipped": "products already built"})
            return
        registry = registry_map(session, scan)
        drafts = extract_products(session, scan, limit=get_settings().report_max_products)
        allowed_codes = {entry.code for entry in registry.values()}
        result, source = run_stage(
            llm, job,
            agent="PRODUCT",
            schema=ProductsResult,
            context={
                "documents": [
                    {"title": draft.name, "price": draft.price, "format": draft.format,
                     "promise": draft.promise, "audience": draft.audience}
                    for draft in drafts
                ],
                "allowed_source_codes": sorted(allowed_codes),
            },
            mock={"products": [draft.as_payload() for draft in drafts]},
            validate=lambda payload: ensure_known_codes(
                "PRODUCT", allowed_codes, payload["products"]
            ),
        )
        codes: list[str] = []
        for index, item in enumerate(result["products"], start=1):
            product = Product(
                scan_id=scan.id,
                code=f"P{index:02d}",
                name=item["name"],
                audience=item["audience"],
                trigger_situation=item["trigger_situation"],
                promise=item["promise"],
                format=item["format"],
                price=item["price"],
                procedure=item["procedure"],
                proof=item["proof"],
                boundary=item["boundary"],
                status=item["status"],
                is_primary=item["is_primary"],
                source_codes=item["source_codes"],
            )
            session.add(product)
            codes.append(product.code)
        session.flush()
        save_annotation(session, job, "scan", scan.id, {"products": codes}, source=source)
        return

    if job.agent == "EVIDENCE":
        entry = registry_map(session, scan).get(job.object_id)
        if entry is None:
            raise ValueError("Source is not registered")
        settings = get_settings()
        used = session.scalar(select(func.count(EvidenceRecord.id)).where(EvidenceRecord.scan_id == scan.id)) or 0
        remaining = settings.report_max_statements - int(used)
        if remaining <= 0:
            save_annotation(session, job, "source", job.object_id, {"skipped": "statement budget exhausted"})
            return
        propositions = _propositions_for_source(session, job.object_id)
        # Per-source and global caps, applied to the model as well: a run that
        # returned 32 claims for one page broke the configured statement budget.
        limit = min(remaining, 8)
        drafts = select_claims(propositions, limit=limit, source_code=entry.code)
        result, source = run_stage(
            llm, job,
            agent="EVIDENCE",
            schema=EvidenceResult,
            context={
                "source": {"code": entry.code, "url": entry.url, "currency": entry.currency},
                "propositions": [{"id": str(item.id), "text": item.text} for item in propositions[:40]],
                "allowed_source_codes": [entry.code],
            },
            mock={"claims": [draft.as_payload() for draft in drafts]},
            validate=lambda payload: ensure_known_codes(
                "EVIDENCE", {entry.code}, payload["claims"]
            ),
        )
        allowed_codes = {entry.code}
        claims: list[ClaimDraft] = []
        for item in result["claims"][:limit]:
            claims.append(ClaimDraft(
                statement=item["statement"],
                relevance=item["relevance"],
                confidence=item["confidence"],
                source_codes=item["source_codes"],
            ))
        saved = persist_claims(session, scan, entry, claims)
        save_annotation(session, job, "source", job.object_id, saved, source=source)
        return

    if job.agent == "LANGUAGE":
        registry = registry_map(session, scan)
        drafts = extract_language(session, scan, registry)
        allowed_codes = {entry.code for entry in registry.values()}
        result, source = run_stage(
            llm, job,
            agent="LANGUAGE",
            schema=LanguageResult,
            context={
                "brand_phrases": [
                    {"topic": draft.topic, "brand": draft.brand,
                     "source_codes": draft.source_codes}
                    for draft in drafts
                ],
                "allowed_source_codes": sorted(allowed_codes),
            },
            mock={"codes": [draft.as_payload() for draft in drafts]},
            validate=lambda payload: ensure_known_codes(
                "LANGUAGE", allowed_codes, payload["codes"]
            ),
        )
        codes = persist_language(session, scan, [
            LanguageDraft(
                topic=item["topic"],
                brand=item["brand"],
                professional=item["professional"],
                lived=item["lived"],
                match_status=item["match_status"],
                is_illustration=item["is_illustration"],
                source_codes=item["source_codes"],
            )
            for item in result["codes"]
        ])
        save_annotation(session, job, "scan", scan.id, {"language_codes": codes}, source=source)
        return

    if job.agent == "ACTANT":
        product = session.get(Product, job.object_id)
        if not product or product.scan_id != scan.id:
            raise ValueError("Product not found")
        if session.scalar(select(ActantMap.id).where(
            ActantMap.scan_id == scan.id,
            ActantMap.product_code == product.code,
        ).limit(1)):
            save_annotation(session, job, "product", product.id, {"skipped": "actant map exists"})
            return
        evidence = list(session.scalars(
            select(EvidenceRecord).where(EvidenceRecord.scan_id == scan.id).order_by(EvidenceRecord.code)
        ))
        draft = build_actant_draft(product, evidence)
        allowed_sources = {entry.code for entry in registry_map(session, scan).values()}
        allowed_evidence = {item.code for item in evidence}
        result, source = run_stage(
            llm, job,
            agent="ACTANT",
            schema=ActantMapResult,
            context={
                "product": {
                    "name": product.name,
                    "audience": product.audience,
                    "promise": product.promise,
                    "trigger": product.trigger_situation,
                    "boundary": product.boundary,
                },
                "evidence": [{"code": item.code, "claim": item.claim} for item in evidence[:10]],
                "allowed_source_codes": sorted(allowed_sources),
                "allowed_evidence_codes": sorted(allowed_evidence),
            },
            mock=draft.payload(),
            validate=lambda payload: _validate_actants(
                payload, allowed_sources, allowed_evidence
            ),
        )
        actant_map = ActantMap(
            scan_id=scan.id,
            code=allocate_code(session, scan.id, "A"),
            product_code=product.code,
            narrative_arc=result["narrative_arc"],
        )
        session.add(actant_map)
        session.flush()
        for item in result["roles"]:
            session.add(ActantRole(
                map_id=actant_map.id,
                role=item["role"],
                object_level=item["object_level"],
                reconstruction=item["reconstruction"],
                status=item["status"],
                alternative=item["alternative"],
                source_codes=item["source_codes"],
                evidence_codes=item["evidence_codes"],
            ))
        if not session.scalar(select(LayerClaim.id).where(
            LayerClaim.scan_id == scan.id,
            LayerClaim.promise == draft.layer_promise,
        ).limit(1)):
            session.add(LayerClaim(
                scan_id=scan.id,
                code=allocate_code(session, scan.id, "T"),
                promise=draft.layer_promise[:1000],
                declared=draft.layer_declared,
                experienced=dict(UNAVAILABLE),
                perceived=dict(UNAVAILABLE),
                match_status="no_data",
                unknown_note=draft.layer_unknown_note,
            ))
        session.flush()
        save_annotation(session, job, "product", product.id, {"map": actant_map.code,
                                                              "roles": len(result["roles"])}, source=source)
        return

    if job.agent == "JOURNEY":
        if session.scalar(select(JourneyStep.id).where(JourneyStep.scan_id == scan.id).limit(1)):
            save_annotation(session, job, "scan", scan.id, {"skipped": "journey exists"})
            return
        products = list(session.scalars(select(Product).where(Product.scan_id == scan.id)))
        evidence = list(session.scalars(
            select(EvidenceRecord).where(EvidenceRecord.scan_id == scan.id).order_by(EvidenceRecord.code)
        ))
        drafts = build_journey_draft(products, evidence)
        result, source = run_stage(
            llm, job,
            agent="JOURNEY",
            schema=JourneyResult,
            context={
                "products": [product.name for product in products],
                "evidence": [item.claim for item in evidence[:10]],
                "allowed_source_codes": sorted(
                    entry.code for entry in registry_map(session, scan).values()
                ),
            },
            mock={"steps": [item.as_payload() for item in drafts]},
            validate=lambda payload: ensure_unique("JOURNEY", payload["steps"], ("step",)),
        )
        allowed_sources = {entry.code for entry in registry_map(session, scan).values()}
        from app.analysis.journey import JourneyDraft

        saved = persist_journey(session, scan, [
            JourneyDraft(
                step=item["step"],
                public_state=item["public_state"],
                obstacle=item["obstacle"],
                check_method=item["check_method"],
                source_codes=[code for code in item["source_codes"] if code in allowed_sources],
                is_observed=False if scan.audit_mode == "desk" else item["is_observed"],
            )
            for item in result["steps"]
        ])
        save_annotation(session, job, "scan", scan.id, {"steps": saved}, source=source)
        return

    if job.agent == "CONSISTENCY":
        if session.scalar(select(ConsistencyCell.id).where(
            ConsistencyCell.scan_id == scan.id,
        ).limit(1)):
            save_annotation(session, job, "scan", scan.id, {"skipped": "consistency exists"})
            return
        products = list(session.scalars(select(Product).where(Product.scan_id == scan.id)))
        evidence = list(session.scalars(
            select(EvidenceRecord).where(EvidenceRecord.scan_id == scan.id).order_by(EvidenceRecord.code)
        ))
        roles = list(session.scalars(
            select(ActantRole).join(ActantMap, ActantRole.map_id == ActantMap.id)
            .where(ActantMap.scan_id == scan.id)
        ))
        journey = list(session.scalars(
            select(JourneyStep).where(JourneyStep.scan_id == scan.id).order_by(JourneyStep.ordinal)
        ))
        drafts = build_consistency_draft(
            products, evidence, roles, journey, audit_mode=scan.audit_mode
        )
        result, source = run_stage(
            llm, job,
            agent="CONSISTENCY",
            schema=ConsistencyResult,
            context={
                "products": [product.name for product in products],
                "actants": [{"role": role.role, "status": role.status} for role in roles],
                "evidence": len(evidence),
                "journey_observed": sum(1 for step in journey if step.is_observed),
            },
            mock={"cells": [draft.as_payload() for draft in drafts]},
            validate=lambda payload: ensure_unique(
                "CONSISTENCY", payload["cells"], ("actant_role", "dimension")
            ),
        )
        allowed_sources = {entry.code for entry in registry_map(session, scan).values()}
        from app.analysis.consistency import CellDraft

        saved = persist_consistency(session, scan, [
            CellDraft(
                actant_role=cell["actant_role"],
                dimension=cell["dimension"],
                status=(
                    "no_data"
                    if scan.audit_mode == "desk" and cell["dimension"] == "experience"
                    else cell["status"]
                ),
                note=cell["note"],
                source_codes=[code for code in cell["source_codes"] if code in allowed_sources],
                counterexample=cell["counterexample"],
            )
            for cell in result["cells"]
        ])
        save_annotation(session, job, "scan", scan.id, {"cells": saved}, source=source)
        return

    if job.agent == "FINDINGS":
        if session.scalar(select(FindingV2.id).where(FindingV2.scan_id == scan.id).limit(1)):
            save_annotation(session, job, "scan", scan.id, {"skipped": "findings exist"})
            return
        products = list(session.scalars(select(Product).where(Product.scan_id == scan.id)))
        evidence = list(session.scalars(
            select(EvidenceRecord).where(EvidenceRecord.scan_id == scan.id).order_by(EvidenceRecord.code)
        ))
        roles = list(session.scalars(
            select(ActantRole).join(ActantMap, ActantRole.map_id == ActantMap.id)
            .where(ActantMap.scan_id == scan.id)
        ))
        cells = list(session.scalars(
            select(ConsistencyCell).where(ConsistencyCell.scan_id == scan.id)
        ))
        journey = list(session.scalars(
            select(JourneyStep).where(JourneyStep.scan_id == scan.id).order_by(JourneyStep.ordinal)
        ))
        landscape = list(session.scalars(
            select(LandscapeEntry).where(LandscapeEntry.scan_id == scan.id)
        ))
        drafts = draft_findings(products, evidence, roles, cells, journey, landscape)
        allowed = {entry.code for entry in registry_map(session, scan).values()} | {
            record.code for record in evidence
        } | {product.code for product in products} | {entry.code for entry in landscape}
        result, source = run_stage(
            llm, job,
            agent="FINDINGS",
            schema=FindingsResult,
            context={
                "products": [{"code": p.code, "price": p.price, "audience": p.audience,
                              "boundary": p.boundary} for p in products],
                "evidence": [{"code": record.code, "claim": record.claim} for record in evidence[:10]],
                "consistency_conflicts": sum(1 for cell in cells if cell.status == "conflict"),
                "landscape": [{"code": entry.code, "promise": entry.promise} for entry in landscape],
                "allowed_basis_codes": sorted(allowed),
                "rules": [
                    "A missing data block is a limitation, not a finding: no competitors"
                    " and no interviews must not become an M/W finding.",
                    "M: an existing offer with a missing link inside it (no price, no"
                    " boundary, no trigger) — cite the product code.",
                    "W: an existing claim without proof — cite its evidence code.",
                    "Every finding cites existing codes only.",
                ],
            },
            mock={"findings": [draft.as_payload() for draft in drafts]},
            validate=lambda payload: _validate_findings(payload, allowed),
        )
        from app.analysis.findings import FindingDraft

        prepared = []
        for item in result["findings"]:
            basis = list(item["basis_codes"])
            prepared.append(FindingDraft(
                kind=item["kind"],
                statement=item["statement"],
                basis_codes=basis,
                consequence=item["consequence"],
                alternative_explanation=item["alternative_explanation"],
                verification=item["verification"],
                priority=item["priority"],
                confidence=item["confidence"],
            ))
        codes = persist_findings(session, scan.id, prepared)
        save_annotation(session, job, "scan", scan.id, {"findings": codes}, source=source)
        return

    if job.agent == "CRITIC":
        findings = list(session.scalars(
            select(FindingV2).where(FindingV2.scan_id == scan.id).order_by(FindingV2.code)
        ))
        registry = list(registry_map(session, scan).values())
        drafts = build_critic_draft(findings, registry)
        result, source = run_stage(
            llm, job,
            agent="CRITIC",
            schema=CriticResult,
            context={
                "findings": [{"code": item.code, "kind": item.kind,
                              "statement": item.statement, "basis": item.basis_codes}
                             for item in findings],
                "sources": [{"code": entry.code, "currency": entry.currency} for entry in registry],
            },
            mock={"items": [draft.as_payload() for draft in drafts]},
        )
        allowed_codes = {finding.code for finding in findings}
        from app.analysis.critic import CriticDraft

        apply_critic_draft(session, findings, [
            CriticDraft(
                code=item["code"],
                alternative_explanation=item["alternative_explanation"],
                needs_review=item["needs_review"],
                note=item["note"],
            )
            for item in result["items"]
            if item["code"] in allowed_codes
        ])
        save_annotation(session, job, "scan", scan.id, {"reviewed": len(result["items"])}, source=source)
        return

    if job.agent == "SCORECARD":
        if session.scalar(select(ScorecardEntry.id).where(
            ScorecardEntry.scan_id == scan.id,
        ).limit(1)):
            save_annotation(session, job, "scan", scan.id, {"skipped": "scorecard exists"})
            return
        products = list(session.scalars(select(Product).where(Product.scan_id == scan.id)))
        evidence = list(session.scalars(
            select(EvidenceRecord).where(EvidenceRecord.scan_id == scan.id).order_by(EvidenceRecord.code)
        ))
        roles = list(session.scalars(
            select(ActantRole).join(ActantMap, ActantRole.map_id == ActantMap.id)
            .where(ActantMap.scan_id == scan.id)
        ))
        cells = list(session.scalars(
            select(ConsistencyCell).where(ConsistencyCell.scan_id == scan.id)
        ))
        findings = list(session.scalars(
            select(FindingV2).where(FindingV2.scan_id == scan.id).order_by(FindingV2.code)
        ))
        landscape = list(session.scalars(
            select(LandscapeEntry).where(LandscapeEntry.scan_id == scan.id)
        ))
        drafts = build_scorecard_draft(
            products, evidence, roles, cells, findings,
            audit_mode=scan.audit_mode, landscape=landscape,
        )
        result, source = run_stage(
            llm, job,
            agent="SCORECARD",
            schema=ScorecardResult,
            context={
                "products": [product.name for product in products],
                "actant_statuses": {role.role: role.status for role in roles},
                "findings": [finding.code for finding in findings],
                "landscape": [entry.code for entry in landscape],
                "audit_mode": scan.audit_mode,
                # The deterministic draft is computed from the registers; the model
                # may keep, adjust or explain it, but scoring from names alone made
                # it mark every criterion N/A.
                "reference_scores": [
                    {"dimension": draft.dimension, "criterion": draft.criterion,
                     "score": draft.score, "rationale": draft.rationale,
                     "na_reason": draft.na_reason, "source_codes": draft.source_codes}
                    for draft in drafts
                ],
                "reference_rule": "reference_scores come from the registers; keep the score "
                                  "unless the evidence above clearly contradicts it",
                "allowed_source_codes": sorted(
                    {entry.code for entry in registry_map(session, scan).values()}
                    | {entry.code for entry in landscape}
                ),
            },
            mock={"items": [draft.as_payload() for draft in drafts]},
            validate=lambda payload: _validate_scorecard(
                payload, scan.audit_mode, {(d.dimension, d.criterion) for d in drafts}
            ),
        )
        allowed_sources = {entry.code for entry in registry_map(session, scan).values()}
        allowed_sources |= {entry.code for entry in landscape}
        prepared: list[ScoreDraft] = []
        for item in result["items"]:
            prepared.append(ScoreDraft(
                dimension=item["dimension"],
                criterion=item["criterion"],
                score=item["score"],
                rationale=item["rationale"],
                na_reason=item["na_reason"],
                confidence=item["confidence"],
                source_codes=[code for code in item["source_codes"] if code in allowed_sources],
            ))
        count = persist_scorecard(session, scan.id, prepared)
        save_annotation(session, job, "scan", scan.id, {"items": count}, source=source)
        return

    if job.agent == "RECOMMEND":
        if session.scalar(select(RecommendationV2.id).where(
            RecommendationV2.scan_id == scan.id,
        ).limit(1)):
            save_annotation(session, job, "scan", scan.id, {"skipped": "recommendations exist"})
            return
        findings = list(session.scalars(
            select(FindingV2).where(FindingV2.scan_id == scan.id).order_by(FindingV2.code)
        ))
        products = list(session.scalars(select(Product).where(Product.scan_id == scan.id)))
        scorecard = list(session.scalars(
            select(ScorecardEntry).where(ScorecardEntry.scan_id == scan.id)
        ))
        registry = list(registry_map(session, scan).values())
        plan = build_plan(
            findings,
            products,
            scorecard,
            audit_mode=scan.audit_mode,
            live_checked=any(entry.live_checked_at is not None for entry in registry),
            landscape_present=bool(session.scalar(
                select(LandscapeEntry.id).where(LandscapeEntry.scan_id == scan.id).limit(1)
            )),
            client_brief=bool(scan.client_brief),
        )
        payload = {
            "recommendations": [
                {
                    "change": draft.change,
                    "rationale": draft.rationale,
                    "expected_effect": draft.expected_effect,
                    "impact": draft.impact,
                    "evidence_confidence": draft.evidence_confidence,
                    "urgency": draft.urgency,
                    "effort": draft.effort,
                    "priority": draft.priority,
                    "decision_class": draft.decision_class,
                    "owner": draft.owner,
                    "finding_codes": draft.finding_codes,
                    "kpi": {
                        "name": draft.kpi.name,
                        "definition": draft.kpi.definition,
                        "baseline": draft.kpi.baseline,
                        "period": draft.kpi.period,
                        "data_source": draft.kpi.data_source,
                        "owner": draft.kpi.owner,
                        "target": draft.kpi.target,
                        "limitations": draft.kpi.limitations,
                    },
                }
                for draft in plan.recommendations
            ],
            "roadmap": plan.roadmap,
            "gate": plan.gate,
            "hypotheses": [
                {"statement": item.statement, "how_to_verify": item.how_to_verify}
                for item in plan.hypotheses
            ],
        }
        result, source = run_stage(
            llm, job,
            agent="RECOMMEND",
            schema=PlanResult,
            context={
                "findings": [{"code": f.code, "statement": f.statement} for f in findings],
                "products": [product.name for product in products],
                "gate_criteria": plan.gate.get("criteria", []),
                "allowed_finding_codes": sorted(f.code for f in findings),
            },
            mock=payload,
            validate=lambda result: ensure_known_codes(
                "RECOMMEND", {finding.code for finding in findings},
                result["recommendations"], field="finding_codes",
            ),
        )
        codes = persist_plan(session, scan, plan_from_payload(result))
        save_annotation(session, job, "scan", scan.id, codes, source=source)
        return

    if job.agent == "NARRATIVE":
        bundle = build_bundle(session, scan.id)
        drafts = build_narrative_draft(bundle)
        refs = allowed_refs(bundle)
        numbers = numeric_corpus(bundle)
        result, source = run_stage(
            llm, job,
            agent="NARRATIVE",
            schema=NarrativeResult,
            context={
                "findings": [{"code": f.code, "statement": f.statement} for f in bundle["findings"]],
                "recommendations": [{"code": r.code, "change": r.change}
                                    for r in bundle["recommendations"]],
                "products": [{"code": p.code, "promise": p.promise} for p in bundle["products"]],
                "gate": None if bundle["gate"] is None else bundle["gate"].decision,
                "allowed_refs": sorted(refs),
                "allowed_section_ids": ["1", "17"],
                "section_id_rule": "keys of `sections` must be the ids \"1\" and \"17\", not titles",
            },
            mock={"sections": drafts},
        )
        sections = {
            section: [dict(paragraph) for paragraph in paragraphs]
            for section, paragraphs in result["sections"].items()
        }
        problems = validate_narrative(sections, refs=refs, numbers=numbers)
        if source == "draft":
            # The narrative is a template built from the registers: valid by
            # construction, but it must never be labelled as model-written.
            if problems:
                raise ValueError(f"Narrative draft failed validation: {problems[:5]}")
            status = "draft"
        elif problems:
            logger.warning(
                "Narrative rejected by the deterministic validator",
                extra={"service": "storyaudit.narrative", "scan_id": str(scan.id),
                       "problems": problems[:8], "problem_count": len(problems)},
            )
            sections = drafts
            status = "fallback"
            if validate_narrative(sections, refs=refs, numbers=numbers):
                raise ValueError("Narrative fallback failed validation")
        else:
            status = "validated"
        persist_narrative(
            session, scan.id, sections,
            status=status,
            model_version=f"code:{scan.schema_version}" if source == "draft" else scan.model_version,
        )
        scan.audit_phase = "narrative_draft"
        save_annotation(session, job, "scan", scan.id, {
            "status": status,
            "sections": sorted(sections),
            "problems": problems,
        }, source=source)
        return

    if job.agent == "LANDSCAPE":
        source = session.get(Source, job.object_id)
        if not source or source.scan_id != scan.id:
            raise ValueError("Source not found")
        entry = registry_map(session, scan).get(source.id)
        if entry is None or entry.role != "competitor":
            raise ValueError("Landscape requires a registered competitor source")
        if session.scalar(select(LandscapeEntry.id).where(
            LandscapeEntry.scan_id == scan.id,
            LandscapeEntry.url == entry.url,
        ).limit(1)):
            save_annotation(session, job, "source", source.id, {"skipped": "landscape exists"})
            return
        grouped = propositions_by_source(session, scan.id).get(source.id, [])
        drafts = [
            draft for draft in build_landscape_draft(session, scan)
            if draft.source_code == entry.code
        ]
        result, stage_source = run_stage(
            llm, job,
            agent="LANDSCAPE",
            schema=LandscapeResult,
            context={
                "competitor": {"url": entry.url, "selection_reason":
                               (entry.entry_metadata or {}).get("selection_reason")},
                "pages": [{"id": str(item.id), "text": item.text} for item in grouped[:40]],
            },
            mock={"items": [draft.as_payload() for draft in drafts]},
        )
        if len(result["items"]) > 1:
            raise ValueError("LANDSCAPE must return one alternative per source")
        prepared: list[LandscapeDraft] = []
        for item in result["items"]:
            if item["source_code"] != entry.code:
                raise ValueError("LANDSCAPE returned a foreign source code")
            if not item["url"].startswith(("http://", "https://")):
                raise ValueError("LANDSCAPE returned an invalid URL")
            if not (item["inclusion_reason"] or "").strip():
                raise ValueError("LANDSCAPE requires an inclusion reason")
            prepared.append(LandscapeDraft(
                name=item["name"],
                url=item["url"],
                source_code=item["source_code"],
                inclusion_reason=item["inclusion_reason"],
                audience=item["audience"],
                opponent=item["opponent"],
                object=item["object"],
                helper_role=item["helper_role"],
                promise=item["promise"],
                proof=item["proof"],
                price=item["price"],
                geographic_scope=item["geographic_scope"],
                territory_notes=item["territory_notes"],
            ))
        codes = persist_landscape(session, scan, prepared)
        save_annotation(session, job, "source", source.id, {"landscape": codes}, source=stage_source)
        return

    raise ValueError(f"Unknown agent: {job.agent}")
