"""Disposable cache for provider answers (see ``LLMCache`` in app/models.py).

Why it exists: a scan is mostly a pure function of the corpus. Retried jobs,
financially identical competitors and sites with shared boilerplate ask the
provider the same questions, and every repeat is paid for. The cache only ever
stores answers that already passed the schema *and* the stage validator; a row
that no longer validates is ignored and overwritten.

The cache never makes the report wrong: a miss costs money, a hit costs nothing.
It is disabled for ``LLM_PROVIDER=mock`` (free anyway) and for reanalysis jobs
(the analyst explicitly asked for a new opinion).

All functions swallow their own errors: a broken cache must degrade to a cache
miss, never fail a job.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from pydantic import BaseModel
from sqlalchemy import text

from app.db import get_session_factory

logger = logging.getLogger("storyaudit.llm_cache")

# Bump when the system prompt or the repair instruction in app/llm.py changes.
# The key cannot read the prompt text itself, so this constant is the invalidation.
PROMPT_VERSION = "prompt_v1"

_schema_fingerprints: dict[type[BaseModel], str] = {}


def _schema_fingerprint(schema: type[BaseModel]) -> str:
    fingerprint = _schema_fingerprints.get(schema)
    if fingerprint is None:
        raw = json.dumps(schema.model_json_schema(), sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        _schema_fingerprints[schema] = fingerprint
    return fingerprint


def _digest(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def request_key(
    *, agent: str, model: str, schema: type[BaseModel], context: dict[str, Any]
) -> str:
    """Key for a whole-stage request (PRODUCT, FINDINGS, NARRATIVE, ...)."""
    return _digest(
        {
            "agent": agent,
            "model": model,
            "prompt": PROMPT_VERSION,
            "schema": _schema_fingerprint(schema),
            "context": context,
        }
    )


def text_key(*, agent: str, model: str, text: str) -> str:
    """Key for one per-proposition answer.

    The batch context carries per-scan proposition ids, so the whole-stage key
    would never match across scans; the normalised text is the stable part.
    """
    return _digest({"agent": agent, "model": model, "prompt": PROMPT_VERSION, "text": text})


def get(key: str) -> dict[str, Any] | None:
    try:
        with get_session_factory()() as session:
            row = session.execute(
                text("SELECT payload FROM llm_cache WHERE key = :key"), {"key": key}
            ).first()
            if row is None:
                return None
            session.execute(
                text("UPDATE llm_cache SET hits = hits + 1 WHERE key = :key"), {"key": key}
            )
            session.commit()
            return row.payload
    except Exception:  # noqa: BLE001 - a broken cache must not fail a job
        logger.warning("LLM cache lookup failed", exc_info=True)
        return None


def get_many(keys: list[str]) -> dict[str, dict[str, Any]]:
    if not keys:
        return {}
    try:
        with get_session_factory()() as session:
            rows = session.execute(
                text("SELECT key, payload FROM llm_cache WHERE key = ANY(:keys)"),
                {"keys": keys},
            ).all()
            if not rows:
                return {}
            found = {row.key: row.payload for row in rows}
            session.execute(
                text("UPDATE llm_cache SET hits = hits + 1 WHERE key = ANY(:keys)"),
                {"keys": list(found)},
            )
            session.commit()
            return found
    except Exception:  # noqa: BLE001
        logger.warning("LLM cache batch lookup failed", exc_info=True)
        return {}


def put(key: str, *, agent: str, model: str, payload: dict[str, Any]) -> None:
    put_many({key: payload}, agent=agent, model=model)


def put_many(entries: dict[str, dict[str, Any]], *, agent: str, model: str = "") -> None:
    if not entries:
        return
    try:
        with get_session_factory()() as session:
            session.execute(
                text(
                    """
                    INSERT INTO llm_cache (key, agent, model, payload, created_at)
                    VALUES (:key, :agent, :model, CAST(:payload AS jsonb), now())
                    ON CONFLICT (key) DO NOTHING
                    """
                ),
                [
                    {
                        "key": key,
                        "agent": agent,
                        "model": model,
                        "payload": json.dumps(payload, ensure_ascii=False),
                    }
                    for key, payload in entries.items()
                ],
            )
            session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("LLM cache write failed", exc_info=True)
