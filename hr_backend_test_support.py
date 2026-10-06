"""
Shared test plumbing for the hr_backend test_hr_backend_*.py files (not a
test module itself - no TestCase here, so unittest discovery skips it).

hr_backend.config.get_settings() / hr_backend.db.get_engine() are
module-level singletons (by design - the real app only ever has one set of
settings/one engine). Tests need a fresh instance per test case, pointed at
an isolated temp SQLite file, so this context manager sets the required env
vars, resets both caches, and restores everything on exit.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from hr_backend import config as hr_config
from hr_backend import db as hr_db


@contextlib.contextmanager
def isolated_hr_backend_env(**extra_env: str):
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "hr_backend_test.db")
        env_overrides = {
            "HR_SESSION_SECRET": "test-secret",
            "HR_BACKEND_DATABASE_URL": f"sqlite:///{db_path}",
            "CACHE_BACKGROUND_REFRESH_ENABLED": "false",
            **extra_env,
        }
        previous = {key: os.environ.get(key) for key in env_overrides}
        os.environ.update(env_overrides)
        hr_config.reset_settings_cache()
        hr_db.reset_engine_cache()
        try:
            import init_hr_backend_db

            init_hr_backend_db.init_db()
            yield db_path
        finally:
            try:
                hr_db.get_engine().dispose()
            except Exception:
                pass
            for key, old_value in previous.items():
                if old_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old_value
            hr_config.reset_settings_cache()
            hr_db.reset_engine_cache()


class HrBackendApiTestCase(unittest.TestCase):
    """Base class for router-level tests: spins up the real FastAPI app
    against an isolated temp SQLite DB, with the dev-login-bypass enabled
    (no real LDAP server needed) so logging in as any username just works."""

    extra_env: dict[str, str] = {}

    def setUp(self):
        self._stack = contextlib.ExitStack()
        self._stack.enter_context(
            isolated_hr_backend_env(
                DEV_LOGIN_BYPASS="true", HR_BACKEND_ENV="development", **self.extra_env
            )
        )
        # Imported here, not at module level - hr_backend.app imports
        # hr_backend.config at import time, and we need the env vars above
        # in place first (reset_settings_cache() alone isn't enough since
        # this is the FIRST import of the module in most test runs).
        from hr_backend.app import app

        self.client = self._stack.enter_context(TestClient(app))

    def tearDown(self):
        self._stack.close()

    def login_as(self, username: str, *, role: str | None = None) -> dict:
        response = self.client.post("/auth/login", json={"username": username, "password": "x"})
        assert response.status_code == 200, response.text
        if role is not None:
            from hr_backend.db import session_scope
            from hr_backend.models import User

            with session_scope() as session:
                user = session.query(User).filter_by(ldap_username=username.lower()).one()
                user.role = role
            # Role changes aren't reflected in the already-issued session
            # token's payload (it only carries user_id) - re-login is not
            # even required since get_current_user re-reads the User row
            # from the DB on every request. Nothing further to do here.
        return response.json()

    def seed_hr_cache(self, *, divisions: list[dict] | None = None, roster: list[dict] | None = None) -> None:
        from hr_backend.cache_keys import CACHE_KEY_DIVISIONS, CACHE_KEY_ROSTER
        from hr_backend.db import session_scope
        from hr_backend.hr_cache import get_generation, write_snapshot

        with session_scope() as session:
            generation = get_generation(session)
            if divisions is not None:
                write_snapshot(session, CACHE_KEY_DIVISIONS, divisions, source_url="test", captured_generation=generation)
            if roster is not None:
                write_snapshot(session, CACHE_KEY_ROSTER, roster, source_url="test", captured_generation=generation)
