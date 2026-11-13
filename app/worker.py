import logging
import signal
import socket
import threading
import time
import uuid

from sqlalchemy import text

from app.agents import execute_job
from app.config import get_settings
from app.db import get_session_factory
from app.logging_config import configure_logging
from app.models import Job
from app.pipeline import schedule_after_success
from app.queue import claim_next_job, complete_job, fail_job, heartbeat, recover_stale_jobs
from app.retention import run_retention

logger = logging.getLogger("storyaudit.worker")
stop_event = threading.Event()


def _stop(signum, frame) -> None:
    logger.info("Received shutdown signal", extra={"service": "worker"})
    stop_event.set()


def _heartbeat_loop(job_id: uuid.UUID, claim_token: uuid.UUID, stopped: threading.Event) -> None:
    settings = get_settings()
    factory = get_session_factory()
    while not stopped.wait(settings.job_heartbeat_seconds):
        with factory() as session:
            job = session.get(Job, job_id)
            if not job or job.claim_token != claim_token:
                return
            if not heartbeat(session, job):
                return


def run_worker() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    settings.validate_worker()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    worker_id = settings.worker_id or f"{socket.gethostname()}-{uuid.uuid4()}"
    factory = get_session_factory()
    logger.info("Worker started", extra={"service": "worker"})
    last_recovery = 0.0
    # Start the retention clock at zero so the first pass happens right away.
    last_retention = 0.0
    while not stop_event.is_set():
        with factory() as session:
            if time.monotonic() - last_recovery > 30:
                recover_stale_jobs(session)
                last_recovery = time.monotonic()
            if settings.retention_enabled and time.monotonic() - last_retention > settings.retention_interval_seconds:
                try:
                    run_retention(session, dry_run=False)
                except Exception:  # retention must never take the worker down
                    session.rollback()
                    logger.exception("Retention failed", extra={"service": "retention"})
                last_retention = time.monotonic()
            session.execute(text("SELECT set_config('app.job_lease_seconds', :lease, true)"), {"lease": str(settings.job_lease_seconds)})
            job = claim_next_job(session, worker_id)
            if not job:
                stop_event.wait(settings.job_poll_interval_seconds)
                continue
            job_id, claim_token = job.id, job.claim_token
            heartbeat_stopped = threading.Event()
            heartbeat_thread = threading.Thread(target=_heartbeat_loop, args=(job_id, claim_token, heartbeat_stopped), daemon=True)
            heartbeat_thread.start()
            started = time.monotonic()
            try:
                execute_job(session, job)
                if complete_job(session, job):
                    schedule_after_success(session, job)
                    session.commit()
                    outcome = "succeeded"
                else:
                    session.rollback()
                    outcome = "ownership_lost"
            except Exception as exc:
                session.rollback()
                session.refresh(job)
                fail_job(session, job, exc)
                logger.exception("Job failed", extra={"service": "worker", "job_id": str(job_id)})
                outcome = "failed"
            finally:
                heartbeat_stopped.set()
                heartbeat_thread.join(timeout=2)
            logger.info(
                "Job processed",
                extra={"service": "worker", "job_id": str(job_id), "agent": job.agent, "duration": time.monotonic() - started, "outcome": outcome},
            )
    logger.info("Worker stopped", extra={"service": "worker"})


if __name__ == "__main__":
    run_worker()
