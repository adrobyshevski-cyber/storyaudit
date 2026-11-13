import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Job, JobDependency, Scan, StageRun, now
from app.utils import stable_hash


def create_stage(
    session: Session,
    scan: Scan,
    agent: str,
    scope_type: str,
    scope_id: uuid.UUID,
    scope_hash: str,
    generation: int,
) -> StageRun:
    existing = session.scalar(
        select(StageRun).where(
            StageRun.scan_id == scan.id,
            StageRun.agent == agent,
            StageRun.analysis_generation == generation,
            StageRun.scope_type == scope_type,
            StageRun.scope_id == scope_id,
            StageRun.scope_hash == scope_hash,
        )
    )
    if existing:
        return existing
    stage = StageRun(
        scan_id=scan.id,
        agent=agent,
        analysis_generation=generation,
        scope_type=scope_type,
        scope_id=scope_id,
        scope_hash=scope_hash,
    )
    session.add(stage)
    session.flush()
    return stage


def enqueue(
    session: Session,
    scan: Scan,
    agent: str,
    object_type: str,
    object_id: uuid.UUID,
    *,
    generation: int,
    job_type: str = "initial",
    issue_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    snapshot: dict[str, Any] | None = None,
    key: str | None = None,
    depends_on: list[uuid.UUID] | None = None,
) -> Job:
    snapshot = snapshot or {}
    scope_hash = stable_hash({"object_type": object_type, "object_id": object_id, "snapshot": snapshot})
    if key is None:
        if generation == 1 and job_type == "initial":
            key = f"{scan.id}:{agent}:{scope_hash}:{scan.prompt_version}"
        else:
            key = f"{scan.id}:{agent}:{object_type}:{object_id}:{generation}:{scan.prompt_version}"
    existing = session.scalar(select(Job).where(Job.idempotency_key == key))
    if existing:
        return existing
    stage = create_stage(session, scan, agent, object_type, object_id, scope_hash, generation)
    job = Job(
        scan_id=scan.id,
        stage_run_id=stage.id,
        analysis_generation=generation,
        agent=agent,
        job_type=job_type,
        object_type=object_type,
        object_id=object_id,
        issue_id=issue_id,
        reanalysis_request_id=request_id,
        scope_hash=scope_hash,
        idempotency_key=key,
        input_snapshot=snapshot,
        max_attempts=get_settings().job_max_attempts,
    )
    session.add(job)
    try:
        with session.begin_nested():
            session.flush()
            stage.expected_job_count += 1
    except IntegrityError:
        return session.scalar(select(Job).where(Job.idempotency_key == key))
    for parent_id in depends_on or []:
        session.add(JobDependency(job_id=job.id, depends_on_job_id=parent_id))
    session.flush()
    return job


def claim_next_job(session: Session, worker_id: str) -> Job | None:
    row = session.execute(text("SELECT * FROM claim_next_job(:worker)"), {"worker": worker_id}).mappings().first()
    if row is None:
        return None
    # ``populate_existing`` matters: if this session already had the job in its
    # identity map, a plain get() would hand back pre-claim values (status
    # "pending", no claim token) and the caller's complete_job() would silently
    # fail the fencing check.
    job = session.get(Job, row["id"], populate_existing=True)
    if job:
        scan = session.get(Scan, job.scan_id)
        if scan and scan.status == "queued":
            scan.status = "running"
    session.commit()
    return job


def heartbeat(session: Session, job: Job) -> bool:
    lease = datetime.now(timezone.utc) + timedelta(seconds=get_settings().job_lease_seconds)
    result = session.execute(
        update(Job)
        .where(Job.id == job.id, Job.claim_token == job.claim_token, Job.status == "running")
        .values(heartbeat_at=now(), lease_expires_at=lease)
    )
    session.commit()
    return result.rowcount == 1


def complete_job(session: Session, job: Job) -> bool:
    owned = session.scalar(
        select(Job).where(
            Job.id == job.id,
            Job.claim_token == job.claim_token,
            Job.analysis_generation == job.analysis_generation,
            Job.status == "running",
        )
    )
    if not owned:
        return False
    owned.status = "succeeded"
    owned.finished_at = now()
    owned.lease_expires_at = None
    completed, expected = session.execute(
        update(StageRun)
        .where(StageRun.id == owned.stage_run_id)
        .values(completed_job_count=StageRun.completed_job_count + 1)
        .returning(StageRun.completed_job_count, StageRun.expected_job_count)
    ).one()
    if completed == expected:
        session.execute(update(StageRun).where(StageRun.id == owned.stage_run_id).values(status="succeeded", completed_at=now()))
    return True


def fail_job(session: Session, job: Job, exc: Exception) -> None:
    owned = session.scalar(select(Job).where(Job.id == job.id, Job.claim_token == job.claim_token, Job.status == "running"))
    if not owned:
        session.rollback()
        return
    owned.last_error_type = type(exc).__name__
    owned.last_error_message = str(exc)[:4000]
    if getattr(exc, "retryable", False) and owned.attempt_count < owned.max_attempts:
        owned.status = "pending"
        owned.available_at = now() + timedelta(seconds=min(60, 2**owned.attempt_count))
        owned.claim_token = None
        owned.lease_expires_at = None
    else:
        owned.status = "failed"
        owned.finished_at = now()
        stage = session.get(StageRun, owned.stage_run_id)
        stage.status = "failed"
        scan = session.get(Scan, owned.scan_id)
        scan.status = "failed"
        scan.error_summary = f"{owned.agent}: {type(exc).__name__}"
        session.execute(
            update(Job)
            .where(Job.scan_id == scan.id, Job.analysis_generation == owned.analysis_generation, Job.status == "pending")
            .values(status="cancelled", finished_at=now())
        )
    session.commit()


def recover_stale_jobs(session: Session) -> int:
    stale = list(session.scalars(select(Job).where(Job.status == "running", Job.lease_expires_at < now())))
    for job in stale:
        if job.attempt_count >= job.max_attempts:
            job.status = "failed"
            scan = session.get(Scan, job.scan_id)
            scan.status = "failed"
            scan.error_summary = f"{job.agent}: lease expired"
        else:
            job.status = "pending"
            job.available_at = now()
            job.claim_token = None
            job.claimed_by = None
            job.lease_expires_at = None
    session.commit()
    return len(stale)
