"""
hr_backend.hr_cache tests: freshness policy (fresh/stale/unavailable/
preparing), a failed refresh never overwriting a prior good payload, the
generation guard discarding a stale in-flight write after an admin clears
the cache, and admin_clear_cache touching only HR cache rows.
"""

import datetime
import unittest

from hr_backend_test_support import isolated_hr_backend_env
from hr_backend.db import session_scope
from hr_backend.hr_cache import (
    admin_clear_cache,
    classify_freshness,
    get_generation,
    read_with_policy,
    record_error,
    write_snapshot,
)
from hr_backend.models import Committee, HrCacheSnapshot


class FreshnessClassificationTests(unittest.TestCase):
    def test_no_snapshot_is_preparing(self):
        self.assertEqual(
            classify_freshness(None, fresh_ttl_seconds=60, max_served_age_seconds=3600), "preparing"
        )

    def test_fresh_within_ttl(self):
        snap = HrCacheSnapshot(cache_key="k", source_url="x", mapping_version="v1", payload=[1], updated_at=datetime.datetime.utcnow())
        self.assertEqual(classify_freshness(snap, fresh_ttl_seconds=60, max_served_age_seconds=3600), "fresh")

    def test_stale_beyond_ttl_within_max_age(self):
        snap = HrCacheSnapshot(
            cache_key="k", source_url="x", mapping_version="v1", payload=[1],
            updated_at=datetime.datetime.utcnow() - datetime.timedelta(seconds=120),
        )
        self.assertEqual(classify_freshness(snap, fresh_ttl_seconds=60, max_served_age_seconds=3600), "stale")

    def test_unavailable_beyond_max_served_age(self):
        snap = HrCacheSnapshot(
            cache_key="k", source_url="x", mapping_version="v1", payload=[1],
            updated_at=datetime.datetime.utcnow() - datetime.timedelta(seconds=7200),
        )
        self.assertEqual(classify_freshness(snap, fresh_ttl_seconds=60, max_served_age_seconds=3600), "unavailable")


class CachePersistenceTests(unittest.TestCase):
    def test_write_then_read(self):
        with isolated_hr_backend_env():
            with session_scope() as s:
                write_snapshot(s, "k", [{"a": 1}], source_url="x", captured_generation=get_generation(s))
            with session_scope() as s:
                result = read_with_policy(s, "k", fresh_ttl_seconds=900, max_served_age_seconds=86400)
            self.assertEqual(result.freshness, "fresh")
            self.assertEqual(result.payload, [{"a": 1}])

    def test_error_never_overwrites_good_payload(self):
        with isolated_hr_backend_env():
            with session_scope() as s:
                write_snapshot(s, "k", [{"good": True}], source_url="x", captured_generation=get_generation(s))
            with session_scope() as s:
                record_error(s, "k")
            with session_scope() as s:
                row = s.get(HrCacheSnapshot, "k")
                self.assertEqual(row.status, "error")
                self.assertEqual(row.payload, [{"good": True}])  # untouched, never replaced by an empty result

    def test_error_on_cold_cache_leaves_no_row(self):
        with isolated_hr_backend_env():
            with session_scope() as s:
                record_error(s, "never-written")
            with session_scope() as s:
                result = read_with_policy(s, "never-written", fresh_ttl_seconds=900, max_served_age_seconds=86400)
            self.assertEqual(result.freshness, "preparing")  # never a misleading empty success

    def test_generation_guard_discards_stale_write(self):
        with isolated_hr_backend_env():
            with session_scope() as s:
                captured_generation = get_generation(s)
            with session_scope() as s:
                admin_clear_cache(s)  # bumps generation, simulating an admin invalidation mid-flight
            with session_scope() as s:
                written = write_snapshot(s, "k", [{"stale": True}], source_url="x", captured_generation=captured_generation)
            self.assertFalse(written)
            with session_scope() as s:
                self.assertIsNone(s.get(HrCacheSnapshot, "k"))

    def test_admin_clear_cache_only_touches_cache_rows(self):
        with isolated_hr_backend_env():
            with session_scope() as s:
                write_snapshot(s, "k", [{"a": 1}], source_url="x", captured_generation=get_generation(s))
                s.add(Committee(name_ar="لجنة"))
            with session_scope() as s:
                cleared = admin_clear_cache(s)
                self.assertEqual(cleared, 1)
            with session_scope() as s:
                self.assertEqual(s.query(Committee).count(), 1)  # business record untouched


if __name__ == "__main__":
    unittest.main()
