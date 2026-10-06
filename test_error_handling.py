"""
Error Handling Tests
Verifies how the system handles a query against a column that doesn't exist,
using a throwaway SQLite database (created in a temp dir, never left behind).

This was previously a print-only demo script; it's now real assertions so it
can run in CI. See test_security.py for the broader Phase 3 security
hardening test suite (injection handling, concept id validation, SQL
rejection, error message shape, air-gapped startup).
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path


def _create_customers_db_without_country(db_path: Path) -> None:
    """Create a customers table that intentionally has no 'country' column."""
    conn = sqlite3.connect(db_path)
    try:
        c = conn.cursor()
        c.execute("DROP TABLE IF EXISTS customers")
        c.execute(
            """CREATE TABLE customers (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                registration_date TEXT
            )"""
        )
        customers = [
            (1, "John Doe", "john@example.com", "+1-555-0101", "2023-01-15"),
            (2, "Jane Smith", "jane@example.com", "+1-555-0102", "2023-02-20"),
            (3, "Ahmed Hassan", "ahmed@example.com", "+20-555-0103", "2023-03-10"),
            (4, "Maria Garcia", "maria@example.com", "+34-555-0104", "2023-04-05"),
            (5, "Li Wei", "li@example.com", "+86-555-0105", "2023-05-12"),
        ]
        c.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?)", customers)
        conn.commit()
    finally:
        conn.close()


class MissingColumnErrorHandlingTests(unittest.TestCase):
    """What happens when generated SQL references a column that doesn't exist."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "customers_no_country.db"
        _create_customers_db_without_country(self.db_path)

    def tearDown(self):
        self._tmp.cleanup()

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def test_query_on_existing_column_succeeds(self):
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM customers WHERE email LIKE '%@gmail%'")
            results = cur.fetchall()
            self.assertEqual(results, [])  # none of the fixtures use gmail
        finally:
            conn.close()

    def test_query_on_missing_column_raises_operational_error(self):
        conn = self._connect()
        try:
            cur = conn.cursor()
            with self.assertRaises(sqlite3.OperationalError) as ctx:
                cur.execute("SELECT * FROM customers WHERE country = 'USA'")
            self.assertIn("no such column", str(ctx.exception))
        finally:
            conn.close()

    def test_available_columns_do_not_include_country(self):
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA table_info(customers)")
            columns = {row[1] for row in cur.fetchall()}
            self.assertNotIn("country", columns)
            self.assertEqual(columns, {"id", "name", "email", "phone", "registration_date"})
        finally:
            conn.close()

    def test_queries_without_the_missing_column_work(self):
        conn = self._connect()
        try:
            cur = conn.cursor()

            cur.execute("SELECT * FROM customers")
            self.assertEqual(len(cur.fetchall()), 5)

            cur.execute("SELECT * FROM customers WHERE registration_date LIKE '2023%'")
            self.assertEqual(len(cur.fetchall()), 5)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
