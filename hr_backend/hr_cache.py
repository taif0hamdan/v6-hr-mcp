"""
Durable HR cache, backed by HrCacheSnapshot rows (not process memory - a
restart must not lose the last good snapshot). Policy per the plan:
  - profile: 15 min fresh
  - divisions: 60 min fresh
  - search: 1 min fresh (served from the same cached roster as everything else)
  - roster: 15 min fresh, servable stale up to 24h while a background
    refresh runs; past that it's reported unavailable rather than served
Keys are scoped by source URL + mapping/schema version, so changing
normalize.py's mapping or eligibility rules invalidates old snapshots
automatically rather than silently reusing them under a new meaning.

A failed refresh NEVER overwrites a prior good payload and NEVER writes an
empty array in place of one - see record_error() vs write_snapshot().
"""

from __future__ import annotations

import datetime
import logging
import threading
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session

from hr_backend.config import get_settings
from hr_backend.models import HrCacheSnapshot, Setting

logger = logging.getLogger(__name__)

Freshness = Literal["fresh", "stale", "unavailable", "preparing"]

_GENERATION_KEY = "hr_cache_generation"

# In-process single-flight locks, keyed by cache_key. Coalesces concurrent
# misses within one worker process. NOTE: this does not coalesce across
# multiple worker processes - see background.py's BackgroundJobLease for
# the one case (roster refresh) where that matters, and the module
# docstring in background.py for the multi-worker caveat on ad-hoc misses.
_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def lock_for(cache_key: str) -> threading.Lock:
    with _locks_guard:
        lock = _locks.get(cache_key)
        if lock is None:
            lock = threading.Lock()
            _locks[cache_key] = lock
        return lock


def get_generation(session: Session) -> int:
    row = session.get(Setting, _GENERATION_KEY)
    return int(row.value) if row and row.value else 0


def bump_generation(session: Session) -> int:
    current = get_generation(session)
    new_value = current + 1
    row = session.get(Setting, _GENERATION_KEY)
    if row is None:
        row = Setting(key=_GENERATION_KEY, value=str(new_value))
        session.add(row)
    else:
        row.value = str(new_value)
    return new_value


def read_snapshot(session: Session, cache_key: str) -> HrCacheSnapshot | None:
    return session.get(HrCacheSnapshot, cache_key)


def classify_freshness(
    snapshot: HrCacheSnapshot | None,
    *,
    fresh_ttl_seconds: int,
    max_served_age_seconds: int,
    now: datetime.datetime | None = None,
) -> Freshness:
    if snapshot is None or snapshot.payload is None:
        return "preparing"  # cold start - never report a misleading empty success
    now = now or datetime.datetime.utcnow()
    age = (now - snapshot.updated_at).total_seconds()
    if age <= fresh_ttl_seconds:
        return "fresh"
    if age <= max_served_age_seconds:
        return "stale"
    return "unavailable"


def write_snapshot(
    session: Session,
    cache_key: str,
    payload,
    *,
    source_url: str,
    captured_generation: int,
) -> bool:
    """Writes a good payload, UNLESS an admin invalidated this cache (bumped
    the generation) while this refresh was in flight - in that case the
    write is discarded so a late stale refresh can't repopulate data an
    admin just cleared. Returns True if written, False if discarded."""
    mapping_version = get_settings().cache.mapping_version
    current_generation = get_generation(session)
    if captured_generation != current_generation:
        logger.warning(
            "Discarding stale refresh for %s: captured generation %s != current %s",
            cache_key, captured_generation, current_generation,
        )
        return False

    row = session.get(HrCacheSnapshot, cache_key)
    if row is None:
        row = HrCacheSnapshot(cache_key=cache_key)
        session.add(row)
    row.source_url = source_url
    row.mapping_version = mapping_version
    row.payload = payload
    row.status = "ok"
    row.generation = current_generation
    row.updated_at = datetime.datetime.utcnow()
    return True


def record_error(session: Session, cache_key: str) -> None:
    """A refresh failed - mark it, but do NOT touch `payload`: the last
    known good snapshot (if any) stays servable as stale/unavailable per
    classify_freshness(), never silently replaced by an empty result."""
    row = session.get(HrCacheSnapshot, cache_key)
    if row is None:
        return  # cold start + immediate failure: nothing to preserve, stays "preparing"
    row.status = "error"


@dataclass(frozen=True)
class CacheRead:
    freshness: Freshness
    payload: object | None
    updated_at: datetime.datetime | None
    refreshing: bool


def read_with_policy(
    session: Session,
    cache_key: str,
    *,
    fresh_ttl_seconds: int,
    max_served_age_seconds: int,
    is_refreshing: bool = False,
) -> CacheRead:
    snapshot = read_snapshot(session, cache_key)
    freshness = classify_freshness(
        snapshot,
        fresh_ttl_seconds=fresh_ttl_seconds,
        max_served_age_seconds=max_served_age_seconds,
    )
    payload = snapshot.payload if snapshot and freshness in ("fresh", "stale") else None
    return CacheRead(
        freshness=freshness,
        payload=payload,
        updated_at=snapshot.updated_at if snapshot else None,
        refreshing=is_refreshing or freshness in ("stale", "preparing"),
    )


def admin_clear_cache(session: Session, *, cache_keys: list[str] | None = None) -> int:
    """Clears ONLY HR cache snapshot rows (never business records: committees,
    sessions, evaluations, results are untouched), and bumps the generation
    counter so any in-flight refresh started before this call is discarded
    rather than repopulating what was just invalidated."""
    query = session.query(HrCacheSnapshot)
    if cache_keys is not None:
        query = query.filter(HrCacheSnapshot.cache_key.in_(cache_keys))
    count = query.delete(synchronize_session=False)
    bump_generation(session)
    return count
