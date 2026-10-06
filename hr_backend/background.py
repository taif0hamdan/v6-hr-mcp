"""
Background roster/division refresh job.

Runs on a 60s poll (configurable), each run bounded by its OWN 120s budget
(config.HrApiSettings.bulk_timeout_seconds) - this must never inherit the
10s single-profile budget used elsewhere. Guarded by a BackgroundJobLease
row so that if multiple worker processes are ever deployed, only one of
them actually calls the HR provider's full-roster endpoint at a time
(best-effort on SQLite; use Postgres with its row locking for a true
multi-worker deployment - documented limitation of the SQLite dev default).

Supports bounded retry/backoff and orderly cancellation (see
hr_backend/app.py's shutdown hook, which cancels and awaits this task).
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import uuid

from hr_backend.cache_keys import CACHE_KEY_DIVISIONS, CACHE_KEY_ROSTER
from hr_backend.config import get_settings
from hr_backend.db import session_scope
from hr_backend.hr_client import HrApiError, HrClient
from hr_backend.hr_cache import get_generation, record_error, write_snapshot
from hr_backend.models import BackgroundJobLease
from hr_backend.normalize import build_division_index, classify_membership, normalize_employee

logger = logging.getLogger(__name__)

ROSTER_JOB_NAME = "roster_refresh"

_WORKER_ID = str(uuid.uuid4())


def try_acquire_lease(job_name: str, *, duration_seconds: int) -> bool:
    now = datetime.datetime.utcnow()
    with session_scope() as session:
        row = session.get(BackgroundJobLease, job_name)
        if row is None:
            row = BackgroundJobLease(job_name=job_name)
            session.add(row)
        elif row.lease_until and row.lease_until > now and row.leased_by != _WORKER_ID:
            return False  # another worker currently holds the lease
        row.leased_by = _WORKER_ID
        row.lease_until = now + datetime.timedelta(seconds=duration_seconds)
    return True


def release_lease(job_name: str) -> None:
    with session_scope() as session:
        row = session.get(BackgroundJobLease, job_name)
        if row and row.leased_by == _WORKER_ID:
            row.lease_until = None


def refresh_roster_once(client: HrClient | None = None) -> bool:
    """One bounded refresh cycle. Returns True on success. Never raises -
    failures are logged and recorded via hr_cache.record_error() without
    touching any prior good snapshot."""
    settings = get_settings()
    client = client or HrClient(settings.hr_api)

    if not try_acquire_lease(ROSTER_JOB_NAME, duration_seconds=int(settings.hr_api.bulk_timeout_seconds) + 10):
        logger.info("Roster refresh skipped: lease held by another worker.")
        return False

    try:
        with session_scope() as session:
            captured_generation = get_generation(session)

        try:
            raw_divisions = client.list_divisions()
            raw_employees = client.list_employees()
        except HrApiError as exc:
            logger.error("Roster refresh failed calling HR provider: %s", exc)
            with session_scope() as session:
                record_error(session, CACHE_KEY_DIVISIONS)
                record_error(session, CACHE_KEY_ROSTER)
            return False

        division_index = build_division_index(raw_divisions)
        normalized_employees = []
        for raw in raw_employees:
            try:
                emp = classify_membership(normalize_employee(raw), division_index)
            except ValueError:
                continue  # a single malformed row (missing MIL_ID) never aborts the whole refresh
            normalized_employees.append(emp.__dict__)

        with session_scope() as session:
            write_snapshot(
                session, CACHE_KEY_DIVISIONS, raw_divisions,
                source_url=f"{settings.hr_api.base_url}/divisions",
                captured_generation=captured_generation,
            )
            write_snapshot(
                session, CACHE_KEY_ROSTER, normalized_employees,
                source_url=f"{settings.hr_api.base_url}/employees",
                captured_generation=captured_generation,
            )
        logger.info("Roster refresh OK: %d employees, %d divisions.", len(normalized_employees), len(raw_divisions))
        return True
    finally:
        release_lease(ROSTER_JOB_NAME)


async def roster_refresh_loop(stop_event: asyncio.Event) -> None:
    settings = get_settings()
    backoff_seconds = settings.cache.background_poll_seconds
    max_backoff_seconds = max(backoff_seconds * 8, 600)

    while not stop_event.is_set():
        try:
            ok = await asyncio.wait_for(
                asyncio.to_thread(refresh_roster_once),
                timeout=settings.hr_api.bulk_timeout_seconds + 10,
            )
            backoff_seconds = settings.cache.background_poll_seconds if ok else min(
                backoff_seconds * 2, max_backoff_seconds
            )
        except asyncio.TimeoutError:
            logger.error("Roster refresh exceeded its bounded budget - aborting this cycle.")
            backoff_seconds = min(backoff_seconds * 2, max_backoff_seconds)
        except asyncio.CancelledError:
            raise  # orderly shutdown - propagate, don't swallow
        except Exception:
            logger.exception("Unexpected error in roster_refresh_loop")
            backoff_seconds = min(backoff_seconds * 2, max_backoff_seconds)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=backoff_seconds)
        except asyncio.TimeoutError:
            continue  # normal poll interval elapsed
