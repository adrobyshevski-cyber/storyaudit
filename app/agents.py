import uuid
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

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
from app.scans import new_calculation_run
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
    LanguageResult,
    ProductsResult,
)
from app.utils import stable_hash


def _label(value: str, confidence: float = 0.85) -> dict[str, Any]:
    return {"label": value, "confidence": confidence}


def _unknown() -> dict[str, Any]:
    return {"label": "unknown", "confidence": 0.0}


SINGLE_SCHEMAS = {"A2": A2Result, "A3": A3Result, "A4": A4Result}
BATCH_SCHEMAS = {"A2": A2BatchResult, "A3": A3BatchResult, "A4": A4BatchResult}


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


def _annotate_batch(session: Session, job: Job, llm: LLMClient, targets: list[Proposition]) -> None:
    """One provider call per chunk; annotations are still stored per proposition."""
    requested = [str(item.id) for item in targets]
    mocks = {str(item.id): _annotation_mock(job.agent, item) for item in targets}

    result = llm.generate(
        agent=job.agent,
        schema=BATCH_SCHEMAS[job.agent],
        context={"propositions": [{"id": str(item.id), "text": item.text} for item in targets]},
        mock={"items": [{"proposition_id": pid, **mocks[pid]} for pid in requested]},
        validate=lambda payload: _check_batch_answer(requested, payload),
    )
    for item in result["items"]:
        body = {key: value for key, value in item.items() if key != "proposition_id"}
        save_annotation(session, job, "proposition", uuid.UUID(str(item["proposition_id"])), body)


def save_annotation(session: Session, job: Job, object_type: str, object_id: uuid.UUID, payload: dict[str, Any]) -> Annotation:
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
    deterministic = job.agent in {"A0", "A5", "A7", "A8a", "A8b", "PROFILE"}
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
        result = llm.generate(agent="A1", schema=A1Result, context={"blocks": [{"id": str(b.id), "text": b.text} for b in blocks]}, mock=mock)
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
        result = llm.generate(
            agent=job.agent,
            schema=SINGLE_SCHEMAS[job.agent],
            context={"proposition": proposition.text},
            mock=_annotation_mock(job.agent, proposition),
        )
        save_annotation(session, job, "proposition", proposition.id, result)
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
            result = llm.generate(
                agent="A6",
                schema=A6Result,
                context={"profile": profile.payload, "evidence": [{"id": str(p.id), "text": p.text} for p in propositions]},
                mock={"features": features},
            )
        else:
            result = {"features": features}
        allowed = set(str(p.id) for p in propositions)
        for feature in result["features"].values():
            if not set(feature["evidence_proposition_ids"]).issubset(allowed):
                raise ValueError("A6 returned foreign evidence")
        save_annotation(session, job, "source", job.object_id, result)
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
        result = llm.generate(
            agent="PRODUCT",
            schema=ProductsResult,
            context={
                "documents": [
                    {"title": draft.name, "price": draft.price, "format": draft.format,
                     "promise": draft.promise, "audience": draft.audience}
                    for draft in drafts
                ],
            },
            mock={"products": [draft.as_payload() for draft in drafts]},
        )
        codes: list[str] = []
        for index, item in enumerate(result["products"], start=1):
            if not set(item["source_codes"]).issubset(allowed_codes):
                raise ValueError("PRODUCT returned an unknown source code")
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
        save_annotation(session, job, "scan", scan.id, {"products": codes})
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
        drafts = select_claims(propositions, limit=min(remaining, 8), source_code=entry.code)
        result = llm.generate(
            agent="EVIDENCE",
            schema=EvidenceResult,
            context={
                "source": {"code": entry.code, "url": entry.url, "currency": entry.currency},
                "propositions": [{"id": str(item.id), "text": item.text} for item in propositions[:40]],
            },
            mock={"claims": [draft.as_payload() for draft in drafts]},
        )
        allowed_codes = {entry.code}
        claims: list[ClaimDraft] = []
        for item in result["claims"]:
            if not set(item["source_codes"]).issubset(allowed_codes):
                raise ValueError("EVIDENCE returned an unknown source code")
            claims.append(ClaimDraft(
                statement=item["statement"],
                relevance=item["relevance"],
                confidence=item["confidence"],
                source_codes=item["source_codes"],
            ))
        saved = persist_claims(session, scan, entry, claims)
        save_annotation(session, job, "source", job.object_id, saved)
        return

    if job.agent == "LANGUAGE":
        drafts = extract_language(session, scan, registry_map(session, scan))
        result = llm.generate(
            agent="LANGUAGE",
            schema=LanguageResult,
            context={"brand_phrases": [draft.brand for draft in drafts]},
            mock={"codes": [draft.as_payload() for draft in drafts]},
        )
        allowed_codes = {entry.code for entry in registry_map(session, scan).values()}
        for item in result["codes"]:
            if not set(item["source_codes"]).issubset(allowed_codes):
                raise ValueError("LANGUAGE returned an unknown source code")
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
        save_annotation(session, job, "scan", scan.id, {"language_codes": codes})
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
        result = llm.generate(
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
            },
            mock=draft.payload(),
        )
        allowed_sources = {entry.code for entry in registry_map(session, scan).values()}
        allowed_evidence = {item.code for item in evidence}
        seen: set[tuple[str, str | None]] = set()
        for item in result["roles"]:
            key = (item["role"], item["object_level"])
            if key in seen:
                raise ValueError("ACTANT returned a duplicate role")
            seen.add(key)
            if not set(item["source_codes"]).issubset(allowed_sources):
                raise ValueError("ACTANT returned an unknown source code")
            if not set(item["evidence_codes"]).issubset(allowed_evidence):
                raise ValueError("ACTANT returned an unknown evidence code")
            if item["status"] == "F" and not item["source_codes"]:
                raise ValueError("ACTANT fact requires a source")
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
                                                              "roles": len(result["roles"])})
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
        result = llm.generate(
            agent="JOURNEY",
            schema=JourneyResult,
            context={"products": [product.name for product in products],
                     "evidence": [item.claim for item in evidence[:10]]},
            mock={"steps": [item.as_payload() for item in drafts]},
        )
        steps = [item["step"] for item in result["steps"]]
        if len(set(steps)) != len(steps):
            raise ValueError("JOURNEY returned duplicate steps")
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
        save_annotation(session, job, "scan", scan.id, {"steps": saved})
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
        result = llm.generate(
            agent="CONSISTENCY",
            schema=ConsistencyResult,
            context={
                "products": [product.name for product in products],
                "actants": [{"role": role.role, "status": role.status} for role in roles],
                "evidence": len(evidence),
                "journey_observed": sum(1 for step in journey if step.is_observed),
            },
            mock={"cells": [draft.as_payload() for draft in drafts]},
        )
        keys = {(cell["actant_role"], cell["dimension"]) for cell in result["cells"]}
        if len(keys) != len(result["cells"]):
            raise ValueError("CONSISTENCY returned duplicate cells")
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
        save_annotation(session, job, "scan", scan.id, {"cells": saved})
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
        result = llm.generate(
            agent="FINDINGS",
            schema=FindingsResult,
            context={
                "products": [{"code": p.code, "price": p.price, "audience": p.audience,
                              "boundary": p.boundary} for p in products],
                "evidence": [{"code": record.code, "claim": record.claim} for record in evidence[:10]],
                "consistency_conflicts": sum(1 for cell in cells if cell.status == "conflict"),
                "landscape": [{"code": entry.code, "promise": entry.promise} for entry in landscape],
            },
            mock={"findings": [draft.as_payload() for draft in drafts]},
        )
        allowed = {entry.code for entry in registry_map(session, scan).values()} | {
            record.code for record in evidence
        } | {product.code for product in products} | {entry.code for entry in landscape}
        from app.analysis.findings import FindingDraft

        prepared = []
        for item in result["findings"]:
            basis = [code for code in item["basis_codes"] if code in allowed]
            if item["kind"] == "gap" and len(set(basis)) < 2:
                raise ValueError("Gap finding requires two observable sides")
            if not basis:
                raise ValueError("Finding requires a reference")
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
        save_annotation(session, job, "scan", scan.id, {"findings": codes})
        return

    if job.agent == "CRITIC":
        findings = list(session.scalars(
            select(FindingV2).where(FindingV2.scan_id == scan.id).order_by(FindingV2.code)
        ))
        registry = list(registry_map(session, scan).values())
        drafts = build_critic_draft(findings, registry)
        result = llm.generate(
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
        save_annotation(session, job, "scan", scan.id, {"reviewed": len(result["items"])})
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
        result = llm.generate(
            agent="SCORECARD",
            schema=ScorecardResult,
            context={
                "products": [product.name for product in products],
                "actant_statuses": {role.role: role.status for role in roles},
                "findings": [finding.code for finding in findings],
                "landscape": [entry.code for entry in landscape],
                "audit_mode": scan.audit_mode,
            },
            mock={"items": [draft.as_payload() for draft in drafts]},
        )
        allowed_sources = {entry.code for entry in registry_map(session, scan).values()}
        allowed_sources |= {entry.code for entry in landscape}
        prepared: list[ScoreDraft] = []
        for item in result["items"]:
            if item["score"] is None and not item["na_reason"]:
                raise ValueError("Scorecard N/A requires a reason")
            if item["dimension"] == "experience" and scan.audit_mode == "desk":
                raise ValueError("Desk audit cannot score the experience dimension")
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
        save_annotation(session, job, "scan", scan.id, {"items": count})
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
        result = llm.generate(
            agent="RECOMMEND",
            schema=PlanResult,
            context={
                "findings": [{"code": f.code, "statement": f.statement} for f in findings],
                "products": [product.name for product in products],
                "gate_criteria": plan.gate.get("criteria", []),
            },
            mock=payload,
        )
        finding_codes = {finding.code for finding in findings}
        for item in result["recommendations"]:
            if not set(item["finding_codes"]).issubset(finding_codes):
                raise ValueError("RECOMMEND returned an unknown finding code")
        codes = persist_plan(session, scan, plan_from_payload(result))
        save_annotation(session, job, "scan", scan.id, codes)
        return

    if job.agent == "NARRATIVE":
        bundle = build_bundle(session, scan.id)
        drafts = build_narrative_draft(bundle)
        refs = allowed_refs(bundle)
        numbers = numeric_corpus(bundle)
        result = llm.generate(
            agent="NARRATIVE",
            schema=NarrativeResult,
            context={
                "findings": [{"code": f.code, "statement": f.statement} for f in bundle["findings"]],
                "recommendations": [{"code": r.code, "change": r.change}
                                    for r in bundle["recommendations"]],
                "products": [{"code": p.code, "promise": p.promise} for p in bundle["products"]],
                "gate": None if bundle["gate"] is None else bundle["gate"].decision,
            },
            mock={"sections": drafts},
        )
        sections = {
            section: [dict(paragraph) for paragraph in paragraphs]
            for section, paragraphs in result["sections"].items()
        }
        problems = validate_narrative(sections, refs=refs, numbers=numbers)
        status = "validated"
        if problems:
            sections = drafts
            status = "fallback"
            if validate_narrative(sections, refs=refs, numbers=numbers):
                raise ValueError("Narrative fallback failed validation")
        persist_narrative(
            session, scan.id, sections,
            status=status, model_version=scan.model_version,
        )
        scan.audit_phase = "narrative_draft"
        save_annotation(session, job, "scan", scan.id, {
            "status": status,
            "sections": sorted(sections),
            "problems": problems,
        })
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
        result = llm.generate(
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
        save_annotation(session, job, "source", source.id, {"landscape": codes})
        return

    raise ValueError(f"Unknown agent: {job.agent}")
