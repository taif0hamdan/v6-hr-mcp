"""
"The machine" - tests for everything that does NOT require an LLM call.

This covers the deterministic layers: database connectivity, schema
reflection, SQL safety validation (sqlparse + dialect-aware row capping +
statement timeout), and the concepts (search-then-read) store loading real
docs from DOCS_DIR. None of this ever talks to an LLM endpoint.

See test_behavior.py for the companion suite that DOES call the configured
LLM to test the natural-language "behavior layer" (QueryParser /
APIRequestParser).

Two kinds of tests live here:
  - Synthetic tests (always run if sqlalchemy+sqlparse are installed): build
    a disposable temp SQLite database so row-cap/timeout/rejection behavior
    is deterministic and doesn't depend on any real external database being
    reachable.
  - Live tests against the actually configured DATABASE_URL / config.yaml:
    skipped automatically (not failed) if that database isn't reachable from
    wherever this suite runs - e.g. the Oracle HR schema configured via
    DATABASE_URL is only reachable from a network that can see
    192.168.27.49:1521.
"""

import importlib.util
import tempfile
import time
import unittest
from pathlib import Path

HAS_SQLALCHEMY = importlib.util.find_spec("sqlalchemy") is not None
HAS_SQLPARSE = importlib.util.find_spec("sqlparse") is not None
HAS_DB_STACK = HAS_SQLALCHEMY and HAS_SQLPARSE

try:
    import main as server_main
    HAS_MAIN = True
except ImportError:
    HAS_MAIN = False

from mcp_server.concepts import CONCEPT_ID_RE, ConceptStore


def _seed_sqlite_db(db_path: Path, row_count: int) -> None:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT)")
        conn.executemany(
            "INSERT INTO widgets (id, name) VALUES (?, ?)",
            [(i, f"widget-{i}") for i in range(row_count)],
        )
        conn.commit()
    finally:
        conn.close()


@unittest.skipUnless(HAS_DB_STACK, "sqlalchemy and sqlparse required")
class SyntheticSqliteMachineTests(unittest.TestCase):
    """Deterministic, self-contained - never touches the real configured database."""

    def setUp(self):
        from db.adapter import DatabaseAdapter

        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "machine_test.db"
        _seed_sqlite_db(self.db_path, row_count=50)

        self.adapter = DatabaseAdapter(f"sqlite:///{self.db_path}", max_rows=10)
        self.adapter.connect()

    def tearDown(self):
        self.adapter.close()
        self._tmp.cleanup()

    def test_schema_reflection_finds_the_table(self):
        schema = self.adapter.get_schema()
        self.assertIn("widgets", schema["tables"])
        self.assertEqual(schema["database_type"], "sqlite")

    def test_select_without_limit_is_still_capped_at_max_rows(self):
        result = self.adapter.execute_query("SELECT * FROM widgets")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["row_count"], 10)  # max_rows=10, table has 50

    def test_select_with_explicit_limit_is_respected(self):
        result = self.adapter.execute_query("SELECT * FROM widgets LIMIT 3")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["row_count"], 3)

    def test_multi_statement_sql_is_rejected(self):
        result = self.adapter.execute_query("SELECT * FROM widgets; DROP TABLE widgets;")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "QUERY_REJECTED")

    def test_destructive_sql_is_rejected_even_against_a_real_known_table(self):
        for sql in [
            "DELETE FROM widgets",
            "DROP TABLE widgets",
            "UPDATE widgets SET name = 'x'",
            "INSERT INTO widgets (id, name) VALUES (999, 'x')",
        ]:
            with self.subTest(sql=sql):
                result = self.adapter.execute_query(sql)
                self.assertFalse(result["success"])
                self.assertEqual(result["error_code"], "QUERY_REJECTED")
                # The table must be untouched - rejection happens before execution.
                untouched = self.adapter.execute_query("SELECT * FROM widgets LIMIT 1")
                self.assertTrue(untouched["success"])

    def test_error_response_never_contains_sql_text(self):
        bad_sql = "DELETE FROM widgets WHERE name = 'super-secret-marker'"
        result = self.adapter.execute_query(bad_sql)
        self.assertFalse(result["success"])
        # detail/error_code must be safe to surface - never echo the rejected SQL.
        self.assertNotIn("super-secret-marker", str(result))
        self.assertNotIn("DELETE", str(result).upper().replace("ERROR_CODE", ""))


@unittest.skipUnless(HAS_DB_STACK, "sqlalchemy and sqlparse required")
class StatementTimeoutTests(unittest.TestCase):
    """Uses a registered SQLite `sleep()` function for a deterministic slow query."""

    def test_slow_query_is_cancelled_by_the_statement_timeout(self):
        from sqlalchemy import create_engine, event

        from db.adapter import DatabaseAdapter

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "slow.db"
            _seed_sqlite_db(db_path, row_count=1)

            engine = create_engine(f"sqlite:///{db_path}")

            @event.listens_for(engine, "connect")
            def _register_sleep(dbapi_conn, _record):
                dbapi_conn.create_function("sleep", 1, time.sleep)

            adapter = DatabaseAdapter(f"sqlite:///{db_path}", max_rows=10, statement_timeout_seconds=1)
            adapter.engine = engine  # pre-register the sleep() function before use

            started = time.monotonic()
            result = adapter.execute_query("SELECT sleep(3)")
            elapsed = time.monotonic() - started

            self.assertFalse(result["success"])
            self.assertEqual(result["error_code"], "QUERY_TIMEOUT")
            self.assertLess(elapsed, 2.5)  # should return around the 1s timeout, not wait for the 3s sleep

            engine.dispose()


@unittest.skipUnless(HAS_DB_STACK and HAS_MAIN, "sqlalchemy, sqlparse, and a full main.py import required")
class LiveConfiguredDatabaseTests(unittest.TestCase):
    """
    Exercises the ACTUAL configured database (DATABASE_URL / config.yaml's
    database.connection_string - currently the Oracle HR schema). Skips
    (never fails) if that database isn't reachable from here.
    """

    @classmethod
    def setUpClass(cls):
        from db.adapter import DatabaseAdapter

        config = server_main.load_config()
        problems = server_main.validate_config(config)
        if problems:
            raise unittest.SkipTest(f"configuration invalid, skipping live DB tests: {problems}")
        if config.get("mode") != "database":
            raise unittest.SkipTest("server is configured for mode=api, not database")

        db_config = config.get("database", {})
        cls.adapter = DatabaseAdapter(
            db_config["connection_string"],
            db_config.get("max_rows", 1000),
            db_config.get("statement_timeout_seconds", 15),
        )
        try:
            cls.adapter.connect()
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"configured database is not reachable from here: {type(exc).__name__}")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "adapter", None):
            cls.adapter.close()

    def test_reflects_at_least_one_table(self):
        schema = self.adapter.get_schema()
        self.assertGreaterEqual(len(schema["tables"]), 1)

    def _find_a_queryable_table(self) -> str:
        """
        Some databases reflect objects the connected user can't actually
        SELECT from (seen on a real Oracle instance: a visible-but-
        inaccessible "dbtools$execution_history" table). Try each reflected
        table until one actually works, rather than assuming the first one
        reflected is queryable.
        """
        for table_name in self.adapter.schema_cache["tables"]:
            probe = self.adapter.execute_query(f"SELECT * FROM {self.adapter.quote_identifier(table_name)}")
            if probe["success"]:
                return table_name
        self.skipTest("none of the reflected tables were actually queryable by the connected user")

    def test_select_star_on_a_queryable_table_respects_row_cap_and_dialect(self):
        table_name = self._find_a_queryable_table()
        result = self.adapter.execute_query(f"SELECT * FROM {self.adapter.quote_identifier(table_name)}")
        self.assertTrue(result["success"], result)
        self.assertLessEqual(result["row_count"], self.adapter.max_rows)

    def test_destructive_sql_rejected_on_the_real_database(self):
        table_name = self._find_a_queryable_table()
        result = self.adapter.execute_query(f"DELETE FROM {self.adapter.quote_identifier(table_name)}")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "QUERY_REJECTED")

    def test_unqueryable_table_error_never_leaks_ora_code_or_table_name(self):
        """Defense-in-depth: even a real ORA-00942 from the DB must come back masked."""
        for table_name in self.adapter.schema_cache["tables"]:
            result = self.adapter.execute_query(f"SELECT * FROM {self.adapter.quote_identifier(table_name)}")
            if not result["success"]:
                self.assertEqual(result["error_code"], "DB_UNAVAILABLE")
                self.assertNotIn("ORA-", str(result))
                self.assertNotIn(table_name, str(result))
                return
        self.skipTest("every reflected table was queryable - nothing to check here")


class ConceptsRealDocsTests(unittest.TestCase):
    """Loads whatever concept docs are actually configured (DOCS_DIR / database_docs/)."""

    def setUp(self):
        if HAS_MAIN:
            config = server_main.load_config()
            self.docs_dir = config["docs_dir"]
        else:
            self.docs_dir = Path(__file__).parent / "database_docs"
        self.store = ConceptStore(self.docs_dir)
        self.store.load()

    def tearDown(self):
        self.store.close()

    def test_every_loaded_concept_id_is_well_formed(self):
        for concept in self.store.index:
            with self.subTest(concept=concept):
                self.assertRegex(concept["id"], CONCEPT_ID_RE.pattern)
                self.assertTrue(concept["title"])

    def test_search_against_the_real_docs_never_raises_unmasked(self):
        from errors import ToolError

        try:
            results = self.store.search("data", 5)
            self.assertIsInstance(results, list)
        except ToolError:
            pass  # a masked "no keywords"/invalid-input error is an acceptable outcome too


if __name__ == "__main__":
    unittest.main()
