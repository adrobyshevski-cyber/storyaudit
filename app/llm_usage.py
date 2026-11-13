"""Token and cost accounting for LLM calls.

Usage rows are written from a separate short-lived session, so the numbers survive
a rollback of the job that produced them (a failed scan still costs money).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_session_factory
from app.models import Job, LLMUsage

logger = logging.getLogger("storyaudit.llm_usage")

TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens")


class DbUsageRecorder:
    """Persist one ``llm_usage`` row per provider request."""

    def __init__(
        self,
        *,
        scan_id: uuid.UUID,
        job_id: uuid.UUID | None = None,
        stage_run_id: uuid.UUID | None = None,
        agent: str | None = None,
    ) -> None:
        self.scan_id = scan_id
        self.job_id = job_id
        self.stage_run_id = stage_run_id
        self.agent = agent

    def record(self, entry: dict[str, Any]) -> None:
        row = {
            "scan_id": self.scan_id,
            "job_id": self.job_id,
            "stage_run_id": self.stage_run_id,
            "agent": entry.get("agent") or self.agent or "unknown",
            "model": entry.get("model") or "unknown",
            "tier": entry.get("tier") or "smart",
            "purpose": entry.get("purpose") or "initial",
            "attempt": int(entry.get("attempt") or 0),
            "prompt_tokens": int(entry.get("prompt_tokens") or 0),
            "completion_tokens": int(entry.get("completion_tokens") or 0),
            "cached_tokens": int(entry.get("cached_tokens") or 0),
            "total_tokens": int(entry.get("total_tokens") or 0),
            "is_estimated": bool(entry.get("is_estimated")),
            "latency_ms": entry.get("latency_ms"),
        }
        try:
            with get_session_factory()() as session:
                session.add(LLMUsage(**row))
                session.commit()
        except Exception:  # accounting must never break the pipeline
            logger.warning("Failed to persist LLM usage", exc_info=True)


def estimate_cost_usd(prompt_tokens: int, completion_tokens: int) -> float | None:
    settings = get_settings()
    if not settings.llm_price_input_per_mtok and not settings.llm_price_output_per_mtok:
        return None
    cost = prompt_tokens / 1_000_000 * settings.llm_price_input_per_mtok
    cost += completion_tokens / 1_000_000 * settings.llm_price_output_per_mtok
    return round(cost, 6)


def _empty_totals() -> dict[str, Any]:
    return {field: 0 for field in TOKEN_FIELDS} | {"calls": 0, "estimated_calls": 0}


def _add(totals: dict[str, Any], row: Any) -> None:
    for field in TOKEN_FIELDS:
        totals[field] += int(getattr(row, field) or 0)
    totals["calls"] += int(row.calls or 0)
    totals["estimated_calls"] += int(row.estimated_calls or 0)


def usage_summary(session: Session, scan_id: uuid.UUID) -> dict[str, Any]:
    """Aggregate usage for a scan: totals, per agent, per model, estimated cost."""
    columns = [
        *(func.coalesce(func.sum(getattr(LLMUsage, field)), 0).label(field) for field in TOKEN_FIELDS),
        func.count(LLMUsage.id).label("calls"),
        func.count(LLMUsage.id).filter(LLMUsage.is_estimated.is_(True)).label("estimated_calls"),
    ]
    by_agent_rows = session.execute(
        select(LLMUsage.agent, *columns).where(LLMUsage.scan_id == scan_id).group_by(LLMUsage.agent)
    ).all()
    by_model_rows = session.execute(
        select(LLMUsage.model, LLMUsage.tier, *columns)
        .where(LLMUsage.scan_id == scan_id)
        .group_by(LLMUsage.model, LLMUsage.tier)
    ).all()
    repair_calls = session.scalar(
        select(func.count(LLMUsage.id)).where(
            LLMUsage.scan_id == scan_id, LLMUsage.purpose == "repair"
        )
    ) or 0

    totals = _empty_totals()
    by_agent: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    for row in by_agent_rows:
        bucket = by_agent.setdefault(row.agent, _empty_totals())
        _add(bucket, row)
        _add(totals, row)
    for row in by_model_rows:
        bucket = by_model.setdefault(row.model, _empty_totals() | {"tier": row.tier})
        _add(bucket, row)

    for bucket in [totals, *by_agent.values(), *by_model.values()]:
        bucket["cost_usd"] = estimate_cost_usd(bucket["prompt_tokens"], bucket["completion_tokens"])

    return {
        "scan_id": str(scan_id),
        "totals": totals,
        "by_agent": by_agent,
        "by_model": by_model,
        "repair_calls": int(repair_calls),
        "prices": {
            "input_per_mtok": get_settings().llm_price_input_per_mtok,
            "output_per_mtok": get_settings().llm_price_output_per_mtok,
            "currency": "USD",
        },
    }


def cost_by_generation(session: Session, scan_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """Cost per analysis generation, joining usage rows back to their jobs."""
    rows = session.execute(
        select(
            Job.analysis_generation,
            func.sum(LLMUsage.prompt_tokens).label("prompt_tokens"),
            func.sum(LLMUsage.completion_tokens).label("completion_tokens"),
        )
        .join(Job, LLMUsage.job_id == Job.id)
        .where(LLMUsage.scan_id == scan_id)
        .group_by(Job.analysis_generation)
    ).all()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        prompt_tokens = int(row.prompt_tokens or 0)
        completion_tokens = int(row.completion_tokens or 0)
        result[str(row.analysis_generation)] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": estimate_cost_usd(prompt_tokens, completion_tokens),
        }
    return result
