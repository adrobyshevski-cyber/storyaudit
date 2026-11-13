"""Concurrency checks for the queue: no double-claim, fencing, real parallel workers.

These tests exist because the architecture claims two things that are easy to
state and easy to break: ``claim_next_job`` never hands the same job to two
workers, and a worker that lost its lease cannot finish (or heartbeat) the job
somebody else now owns.
"""

import os
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError

from app.agents import execute_job
from app.db import get_session_factory
from app.models import Annotation, Job, Scan, StageRun
from app.pipeline import schedule_after_success
from app.queue import claim_next_job, complete_job, heartbeat
from app.schemas import DocumentCreate, ScanCreate, SourceCreate
from app.scans import add_source, create_scan, run_scan

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARAGRAPHS = 8


def require_db():
    if not os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    session = get_session_factory()()
    try:
        session.execute(text("SELECT 1"))
    except OperationalError:
        session.close()
        pytest.skip("PostgreSQL is not available")
    return session


@pytest.fixture(autouse=True)
def clean_queue():
    """The queue is global: leftover jobs from another test would be claimed here."""
    if not os.environ.get("TEST_DATABASE_URL"):
        yield
        return
    session = get_session_factory()()
    try:
        for statement in (
            "DELETE FROM job_dependencies",
            "DELETE FROM llm_usage",
            "DELETE FROM jobs",
            "DELETE FROM stage_runs",
        ):
            session.execute(text(statement))
        session.commit()
    except OperationalError:
        session.rollback()
    finally:
        session.close()
    yield


def seed_scan(session) -> Scan:
    scan = create_scan(session, ScanCreate(title=f"Concurrency {uuid.uuid4().hex[:6]}"))
    body = "\n\n".join(
        f"Statement {index}: teams lose feedback from customers." for index in range(PARAGRAPHS)
    )
    add_source(session, scan.id, SourceCreate(
        name="Concurrency source",
        documents=[DocumentCreate(title="Doc", text=body)],
    ))
    run_scan(session, scan.id)
    return scan


def wait_for_scan(session, scan_id: uuid.UUID, timeout: float = 120.0) -> Scan:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        session.expire_all()
        scan = session.get(Scan, scan_id)
        if scan.status in {"succeeded", "failed"}:
            return scan
        time.sleep(0.2)
    raise AssertionError("scan did not finish in time")


def test_four_claimers_process_every_job_exactly_once(monkeypatch):
    session = require_db()
    # One job per proposition: more rows means real contention between claimers.
    monkeypatch.setenv("ANNOTATION_BATCH_SIZE", "1")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        scan = seed_scan(session)
        factory = get_session_factory()
        claimed: list[uuid.UUID] = []
        failures: list[BaseException] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            name = f"thread-{index}"
            with factory() as worker_session:
                idle_rounds = 0
                while True:
                    worker_session.expire_all()
                    current = worker_session.get(Scan, scan.id)
                    if current.status in {"succeeded", "failed"}:
                        return
                    job = claim_next_job(worker_session, name)
                    if job is None:
                        idle_rounds += 1
                        if idle_rounds > 600:  # ~30s of empty polls
                            raise AssertionError(f"{name} starved")
                        time.sleep(0.05)
                        continue
                    idle_rounds = 0
                    with lock:
                        claimed.append(job.id)
                    try:
                        execute_job(worker_session, job)
                        if complete_job(worker_session, job):
                            schedule_after_success(worker_session, job)
                            worker_session.commit()
                        else:
                            worker_session.rollback()
                            with lock:
                                failures.append(AssertionError(f"{name} lost ownership of {job.id}"))
                    except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread
                        worker_session.rollback()
                        with lock:
                            failures.append(exc)
                        return

        threads = [threading.Thread(target=worker, args=(index,), daemon=True) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=180)

        assert not failures, failures[:3]
        scan = wait_for_scan(session, scan.id)
        assert scan.status == "succeeded"

        # 1. Nobody saw the same job twice.
        duplicates = [job_id for job_id, count in Counter(claimed).items() if count > 1]
        assert not duplicates, f"{len(duplicates)} jobs were claimed more than once"

        # 2. Every job in the database was processed, exactly once.
        jobs = list(session.scalars(select(Job).where(Job.scan_id == scan.id)))
        # Guard the test itself: too few jobs would make the contention meaningless.
        assert len(jobs) >= PARAGRAPHS * 3, f"only {len(jobs)} jobs were created"
        assert len(jobs) == len(claimed)
        assert {job.id for job in jobs} == set(claimed)
        assert all(job.status == "succeeded" for job in jobs)
        # claim_next_job increments attempt_count, so "claimed once, never retried"
        # means exactly 1 on every job.
        assert all(job.attempt_count == 1 for job in jobs), [
            (job.agent, job.attempt_count) for job in jobs if job.attempt_count != 1
        ]

        # 3. Stage barriers agree with reality.
        stages = list(session.scalars(
            select(StageRun).where(StageRun.scan_id == scan.id, StageRun.status != "succeeded")
        ))
        assert not stages, [(stage.agent, stage.status) for stage in stages]
        mismatched = session.execute(
            select(StageRun.agent, StageRun.completed_job_count, StageRun.expected_job_count).where(
                StageRun.scan_id == scan.id,
                StageRun.completed_job_count != StageRun.expected_job_count,
            )
        ).all()
        assert not mismatched, mismatched

        # 4. Exactly one current annotation per (agent, object) pair.
        doubled = session.execute(
            select(Annotation.agent, Annotation.object_id, func.count(Annotation.id))
            .where(Annotation.scan_id == scan.id, Annotation.is_current.is_(True))
            .group_by(Annotation.agent, Annotation.object_id)
            .having(func.count(Annotation.id) > 1)
        ).all()
        assert not doubled, doubled
    finally:
        session.close()


def test_claim_is_atomic_under_contention():
    """Pure claim path: four claimers draining N jobs never see the same id."""
    session = require_db()
    factory = get_session_factory()
    try:
        scan = seed_scan(session)
        stage_id = session.scalar(
            select(StageRun.id).where(StageRun.scan_id == scan.id).limit(1)
        )
        assert stage_id is not None
        probe_jobs = [
            Job(
                scan_id=scan.id, stage_run_id=stage_id, analysis_generation=1,
                agent="PROBE", job_type="initial", object_type="scan", object_id=scan.id,
                scope_hash=f"probe{index:059d}",
                idempotency_key=f"probe-{scan.id}-{index}",
                status="pending", max_attempts=3,
            )
            for index in range(40)
        ]
        session.add_all(probe_jobs)
        session.commit()
        probe_ids = {job.id for job in probe_jobs}

        claimed: list[uuid.UUID] = []
        lock = threading.Lock()

        def claimer(index: int) -> None:
            name = f"claimer-{index}"
            with factory() as claim_session:
                while True:
                    job = claim_next_job(claim_session, name)
                    if job is None:
                        return
                    with lock:
                        claimed.append(job.id)
                    # Not completing on purpose: the row stays 'running', which is
                    # enough to assert claim exclusivity.

        threads = [threading.Thread(target=claimer, args=(i,), daemon=True) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        duplicates = [job_id for job_id, count in Counter(claimed).items() if count > 1]
        assert not duplicates, f"{len(duplicates)} double-claims"
        assert probe_ids.issubset(set(claimed)), "some probe jobs were never claimed"
        assert len(claimed) == len(set(claimed))
    finally:
        session.close()


def test_stale_claim_token_cannot_complete_or_heartbeat():
    """Two sessions model two workers: the old one keeps a pre-recovery view."""
    session = require_db()
    old_worker = get_session_factory()()
    new_worker = get_session_factory()()
    try:
        seed_scan(session)
        claimed = claim_next_job(old_worker, "worker-old")
        assert claimed is not None
        old_job_id = claimed.id

        # The worker stops sending heartbeats and its lease expires.
        claimed.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        old_worker.commit()

        from app.queue import recover_stale_jobs

        assert recover_stale_jobs(new_worker) >= 1
        taken = claim_next_job(new_worker, "worker-new")
        assert taken is not None and taken.id == old_job_id
        new_token = taken.claim_token
        assert new_token is not None

        # The old worker still holds its object with the previous token and tries
        # to finish. Fencing must reject it without writing anything.
        assert complete_job(old_worker, claimed) is False
        assert heartbeat(old_worker, claimed) is False

        new_worker.expire_all()
        row = new_worker.get(Job, old_job_id)
        assert row.status == "running"
        assert row.claim_token == new_token
        assert complete_job(new_worker, row) is True
    finally:
        old_worker.close()
        new_worker.close()
        session.close()


def test_expired_lease_is_recovered_and_finished_by_another_worker():
    from app.queue import recover_stale_jobs

    session = require_db()
    try:
        seed_scan(session)
        job = claim_next_job(session, "worker-doomed")
        assert job is not None
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        session.commit()

        assert recover_stale_jobs(session) >= 1
        session.expire_all()
        recovered = session.get(Job, job.id)
        assert recovered.status == "pending"
        assert recovered.claim_token is None

        other = claim_next_job(session, "worker-alive")
        assert other is not None and other.id == job.id
        execute_job(session, other)
        assert complete_job(session, other) is True
        session.commit()
        session.expire_all()
        assert session.get(Job, job.id).status == "succeeded"
    finally:
        session.close()


@pytest.mark.skipif(sys.platform == "win32", reason="process-level run is covered on Linux CI")
def test_four_worker_processes_drain_a_scan():
    session = require_db()
    processes: list[subprocess.Popen] = []
    try:
        scan = seed_scan(session)
        env = os.environ.copy()
        env.update({
            "APP_ENV": "test",
            "LLM_PROVIDER": "mock",
            "DATABASE_URL": os.environ["TEST_DATABASE_URL"],
            "JOB_POLL_INTERVAL_SECONDS": "0.3",
            "JOB_LEASE_SECONDS": "60",
            "JOB_HEARTBEAT_SECONDS": "5",
            "RETENTION_ENABLED": "false",
        })
        for _ in range(4):
            processes.append(subprocess.Popen(
                [sys.executable, "-m", "app.worker"],
                cwd=REPO_ROOT, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))

        scan = wait_for_scan(session, scan.id, timeout=180)
        assert scan.status == "succeeded"

        jobs = list(session.scalars(select(Job).where(Job.scan_id == scan.id)))
        assert jobs
        assert all(job.status == "succeeded" for job in jobs), [
            (job.agent, job.status, job.last_error_type) for job in jobs if job.status != "succeeded"
        ][:5]
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
        session.close()
