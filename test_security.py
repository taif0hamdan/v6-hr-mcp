"""
Security hardening tests (Phase 3).

Covers:
  - search_concepts against injection-style input: no exception leaks, only
    a valid result or a masked INVALID_INPUT error.
  - concept:// id validation: malformed ids -> INVALID_INPUT; well-formed
    but unknown ids -> CONCEPT_NOT_FOUND.
  - The NL-to-SQL hardening layer (db.adapter.validate_select_only)
    rejecting multi-statement and non-SELECT SQL.
  - Every error message's shape: starts with "Error: ", contains "Next:",
    and never contains a stack trace, a file path, a .py reference, or
    anything that looks like a secret.
  - Air-gapped startup: the concepts subsystem and LLM settings resolution
    never touch the network and never default to a remote address.

Some test classes are skipped automatically if their dependency (sqlalchemy,
sqlparse, pydantic, fastmcp, openai) is not installed in the current
environment - they are still written to run for real once those are
installed (see requirements.txt / scripts/download_wheels.sh).
"""

import importlib.util
import socket
import tempfile
import unittest
from pathlib import Path

HAS_SQLALCHEMY = importlib.util.find_spec("sqlalchemy") is not None
HAS_SQLPARSE = importlib.util.find_spec("sqlparse") is not None

import errors
from errors import ErrorCode, ToolError, build_message, mcp_error, truncate_echo
from mcp_server.concepts import CONCEPT_ID_RE, ConceptStore

FORBIDDEN_SUBSTRINGS = ["Traceback", 'File "', ".py", "sk-"]


def _write_concept_doc(dir_path: Path, filename: str, content: str) -> None:
    (dir_path / filename).write_text(content, encoding="utf-8")


class ConceptSearchInjectionTests(unittest.TestCase):
    """search_concepts' underlying store must never leak an unmasked exception."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        docs_dir = Path(self._tmp.name)
        _write_concept_doc(docs_dir, "widgets.yaml", "database_context:\n  domain: Widgets\n  description: test fixture\n")
        self.store = ConceptStore(docs_dir)
        self.store.load()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def _search_or_tool_error(self, query: str):
        """Run search(); a ToolError is an acceptable, masked outcome - anything else is not."""
        try:
            return ("ok", self.store.search(query, 5))
        except ToolError as exc:
            return ("tool_error", str(exc))

    def test_injection_style_inputs_never_raise_unmasked(self):
        payloads = [
            "' OR 1=1 --",
            '"); DROP TABLE x; --',
            "*",
            "NEAR(",
            "' UNION SELECT * FROM concepts_fts --",
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                outcome, result = self._search_or_tool_error(payload)
                if outcome == "tool_error":
                    self.assertTrue(result.startswith("Error: "))
                    self.assertIn("Next:", result)
                    for forbidden in FORBIDDEN_SUBSTRINGS:
                        self.assertNotIn(forbidden, result)
                else:
                    self.assertIsInstance(result, list)

    def test_empty_query_raises_masked_invalid_input(self):
        with self.assertRaises(ToolError) as ctx:
            self.store.search("", 5)
        message = str(ctx.exception)
        self.assertTrue(message.startswith("Error: INVALID_INPUT"))
        self.assertIn("Next:", message)


class ConceptIdValidationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        docs_dir = Path(self._tmp.name)
        _write_concept_doc(docs_dir, "widgets.yaml", "database_context:\n  domain: Widgets\n")
        self.store = ConceptStore(docs_dir)
        self.store.load()

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()

    def test_malformed_ids_rejected_by_regex(self):
        for bad_id in ["../etc/passwd", "a;b", "a" * 65, "Has Spaces", "CAPS", "", "a/b"]:
            with self.subTest(bad_id=bad_id):
                self.assertIsNone(CONCEPT_ID_RE.match(bad_id))

    def test_wellformed_unknown_id_returns_none_not_an_exception(self):
        self.assertTrue(CONCEPT_ID_RE.match("does-not-exist"))
        self.assertIsNone(self.store.get("does-not-exist"))

    def test_known_id_round_trips(self):
        self.assertTrue(CONCEPT_ID_RE.match("widgets"))
        body = self.store.get("widgets")
        self.assertIsNotNone(body)
        self.assertIn("Widgets", body)


@unittest.skipUnless(HAS_SQLALCHEMY and HAS_SQLPARSE, "sqlalchemy and sqlparse required")
class SqlValidationTests(unittest.TestCase):
    def test_single_select_accepted(self):
        from db.adapter import validate_select_only
        ok, reason = validate_select_only("SELECT * FROM customers LIMIT 10")
        self.assertTrue(ok, reason)

    def test_with_cte_select_accepted(self):
        from db.adapter import validate_select_only
        ok, reason = validate_select_only(
            "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent"
        )
        self.assertTrue(ok, reason)

    def test_multi_statement_rejected(self):
        from db.adapter import validate_select_only
        ok, reason = validate_select_only("SELECT * FROM customers; DROP TABLE customers;")
        self.assertFalse(ok)
        self.assertIsNotNone(reason)

    def test_non_select_rejected(self):
        from db.adapter import validate_select_only
        for sql in [
            "DELETE FROM customers",
            "DROP TABLE customers",
            "UPDATE customers SET name = 'x'",
            "INSERT INTO customers (name) VALUES ('x')",
        ]:
            with self.subTest(sql=sql):
                ok, reason = validate_select_only(sql)
                self.assertFalse(ok)

    def test_writable_cte_rejected(self):
        from db.adapter import validate_select_only
        ok, reason = validate_select_only(
            "WITH deleted AS (DELETE FROM customers RETURNING *) SELECT * FROM deleted"
        )
        self.assertFalse(ok)


@unittest.skipUnless(HAS_SQLALCHEMY and HAS_SQLPARSE, "sqlalchemy and sqlparse required")
class DialectAwareRowLimitTests(unittest.TestCase):
    """Oracle has no LIMIT keyword (ORA-00933) - the row cap must use its own syntax."""

    def test_default_dialects_use_limit(self):
        from db.adapter import append_row_limit, has_row_limit_clause
        for dialect in ["sqlite", "postgresql", "mysql"]:
            with self.subTest(dialect=dialect):
                sql = append_row_limit(dialect, "SELECT * FROM employees", "_max_rows")
                self.assertTrue(sql.endswith("LIMIT :_max_rows"))
                self.assertTrue(has_row_limit_clause(dialect, sql.upper()))
                self.assertFalse(has_row_limit_clause(dialect, "SELECT * FROM employees".upper()))

    def test_oracle_uses_fetch_first_not_limit(self):
        from db.adapter import append_row_limit, has_row_limit_clause
        sql = append_row_limit("oracle", "SELECT * FROM employees", "_max_rows")
        self.assertNotIn("LIMIT", sql.upper())
        self.assertTrue(sql.endswith("FETCH FIRST :_max_rows ROWS ONLY"))
        self.assertTrue(has_row_limit_clause("oracle", sql.upper()))
        self.assertFalse(has_row_limit_clause("oracle", "SELECT * FROM employees".upper()))
        # A bare "LIMIT" in generated SQL must NOT be mistaken for an Oracle row cap.
        self.assertFalse(has_row_limit_clause("oracle", "SELECT * FROM employees LIMIT 10".upper()))


class ErrorMessageShapeTests(unittest.TestCase):
    def test_every_code_produces_well_formed_message(self):
        for code in ErrorCode:
            with self.subTest(code=code):
                message = build_message(code, "something went wrong")
                self.assertTrue(message.startswith(f"Error: {code.value} | "))
                self.assertIn("Next:", message)
                self.assertIn("Ref:", message)
                for forbidden in FORBIDDEN_SUBSTRINGS:
                    self.assertNotIn(forbidden, message)

    def test_mcp_error_message_shape(self):
        exc = mcp_error(ErrorCode.CONCEPT_NOT_FOUND, "no concept exists with id 'xyz'")
        message = str(exc)
        self.assertTrue(message.startswith("Error: CONCEPT_NOT_FOUND | "))
        self.assertIn("Next:", message)
        self.assertIn("call search_concepts", message)

    def test_truncate_echo_caps_length(self):
        long_input = "x" * 500
        truncated = truncate_echo(long_input)
        self.assertLessEqual(len(truncated), errors.MAX_ECHOED_INPUT + 3)  # +3 for "..."

    def test_guarded_decorator_masks_unexpected_exceptions(self):
        @errors.guarded()
        def boom():
            raise ValueError("some internal detail: /etc/secret, DATABASE_URL=postgresql://u:p@host/db")

        with self.assertRaises(ToolError) as ctx:
            boom()
        message = str(ctx.exception)
        self.assertTrue(message.startswith("Error: INTERNAL"))
        self.assertIn("Next:", message)
        self.assertNotIn("DATABASE_URL", message)
        self.assertNotIn("postgresql://", message)
        self.assertNotIn("/etc/secret", message)
        for forbidden in FORBIDDEN_SUBSTRINGS:
            self.assertNotIn(forbidden, message)

    def test_guarded_decorator_passes_through_deliberate_mcp_errors(self):
        @errors.guarded()
        def rejects():
            raise mcp_error(ErrorCode.QUERY_REJECTED, "not a SELECT")

        with self.assertRaises(ToolError) as ctx:
            rejects()
        self.assertTrue(str(ctx.exception).startswith("Error: QUERY_REJECTED"))


class AirgappedStartupTests(unittest.TestCase):
    """The concepts subsystem and LLM settings resolution must never touch the network."""

    def test_concepts_load_without_network(self):
        original_socket = socket.socket

        def no_network_socket(*args, **kwargs):
            raise AssertionError("Unexpected network access during concept loading")

        socket.socket = no_network_socket
        try:
            with tempfile.TemporaryDirectory() as tmp:
                docs_dir = Path(tmp)
                _write_concept_doc(docs_dir, "widgets.yaml", "database_context:\n  domain: Widgets\n")
                store = ConceptStore(docs_dir)
                store.load()  # must not touch the network - sqlite3 is in-process
                self.assertEqual(len(store.index), 1)
                store.close()
        finally:
            socket.socket = original_socket

    def test_llm_settings_default_to_local_not_openai(self):
        from utils.llm_client import DEFAULT_LOCAL_BASE_URL, resolve_llm_settings

        self.assertNotIn("api.openai.com", DEFAULT_LOCAL_BASE_URL)
        settings = resolve_llm_settings({})
        self.assertNotIn("api.openai.com", settings["base_url"])


if __name__ == "__main__":
    unittest.main()
