"""
Universal Database Adapter
Connects to any SQL database via SQLAlchemy and provides schema introspection.

Hardening applied here (see errors.py / docs for the full policy):
  - SQLite connections are opened read-only via the `file:...?mode=ro&uri=true`
    recipe. Non-SQLite dialects rely on a read-only DB user supplied through
    DATABASE_URL — this adapter cannot enforce that itself, it only warns.
  - Every generated query is validated with sqlparse (exactly one statement,
    SELECT or WITH...SELECT) before it reaches the database. The NL-to-SQL
    layer's keyword blacklist (nlp/query_parser.py) remains a second,
    independent layer — this is not a replacement for it.
  - The safety LIMIT is appended as a bound parameter, never interpolated.
  - A best-effort statement timeout cancels long-running queries.
  - Identifiers used in `_get_sample_data` / `_get_row_count` come only from
    SQLAlchemy's own `inspector.get_table_names()` (never from user/LLM
    input), and are additionally checked against that same reflected list
    immediately before use, since SQL does not support binding identifiers
    as parameters.
"""

import logging
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import sqlparse
from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)

DEFAULT_STATEMENT_TIMEOUT_SECONDS = 15

# Identifiers must look like identifiers - a final defense-in-depth check even
# though callers only ever pass names that already came from the reflected
# table list.
_SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class QueryTimeoutError(Exception):
    """Raised when a query exceeds the configured statement timeout."""


def validate_select_only(sql: str) -> Tuple[bool, Optional[str]]:
    """
    Parse `sql` with sqlparse and require exactly one statement, of type
    SELECT (including a leading WITH ... common table expression).

    Deliberately avoids relying on `Statement.get_type()` (its handling of
    WITH/CTE statements has varied across sqlparse releases) and instead
    checks the first keyword and every DML keyword in the statement
    directly, which is stable across versions.

    Returns (True, None) if valid, otherwise (False, reason) where reason is
    a short, safe (no SQL echoed) explanation.
    """
    statements = [s for s in sqlparse.parse(sql) if s.token_first(skip_cm=True) is not None]

    if len(statements) == 0:
        return False, "no SQL statement was generated"
    if len(statements) > 1:
        return False, "multiple SQL statements were generated"

    stmt = statements[0]
    first_token = stmt.token_first(skip_cm=True)
    first_word = (first_token.normalized or "").upper() if first_token else ""

    # Every DML keyword (SELECT/INSERT/UPDATE/DELETE/...) anywhere in the
    # statement - used to reject writable CTEs like
    # "WITH x AS (INSERT INTO t ... RETURNING *) SELECT * FROM x".
    dml_tokens = [tok.normalized.upper() for tok in stmt.flatten() if tok.ttype is sqlparse.tokens.DML]

    if first_word == "SELECT":
        return True, None

    if first_word == "WITH":
        if dml_tokens and all(tok == "SELECT" for tok in dml_tokens):
            return True, None
        return False, "WITH statement does not resolve to a read-only SELECT"

    return False, "statement is not a read-only SELECT"


def _is_known_table_identifier(table_name: str, known_tables: List[str]) -> bool:
    return bool(_SAFE_IDENTIFIER_RE.match(table_name)) and table_name in known_tables


# Dialect-specific row-cap syntax. Oracle has no LIMIT keyword at all (it
# raises ORA-00933) - it uses the ANSI "FETCH FIRST n ROWS ONLY" form
# instead, supported since Oracle 12c (true for the HR sample schema's usual
# hosts: XE 18c/19c/21c/23c). Anything not listed here falls back to LIMIT
# (SQLite, PostgreSQL, MySQL/MariaDB all support it).
_ROW_LIMIT_KEYWORDS = {
    "oracle": "FETCH FIRST",
}


def append_row_limit(dialect_name: str, sql: str, param_name: str) -> str:
    """Append a bound-parameter row cap to `sql`, using the right syntax for `dialect_name`."""
    if dialect_name == "oracle":
        return f"{sql} FETCH FIRST :{param_name} ROWS ONLY"
    return f"{sql} LIMIT :{param_name}"


def has_row_limit_clause(dialect_name: str, sql_upper: str) -> bool:
    """True if `sql_upper` already caps its own row count for this dialect."""
    keyword = _ROW_LIMIT_KEYWORDS.get(dialect_name, "LIMIT")
    return keyword in sql_upper


def build_readonly_sqlite_url(connection_string: str) -> str:
    """
    Rewrite a `sqlite:///path/to.db` URL into the read-only
    `sqlite:///file:path/to.db?mode=ro&uri=true` form SQLAlchemy recognizes.
    Already-read-only or non-file (`:memory:`) URLs are passed through.
    """
    prefix = "sqlite:///"
    if not connection_string.startswith(prefix):
        return connection_string

    path = connection_string[len(prefix):]
    if path in (":memory:", "") or path.startswith("file:"):
        return connection_string

    # Strip any existing query string the caller may have supplied.
    base_path = path.split("?", 1)[0]
    return f"sqlite:///file:{base_path}?mode=ro&uri=true"


class DatabaseAdapter:
    """Universal SQL database adapter with auto-schema detection."""

    def __init__(self, connection_string: str, max_rows: int = 1000, statement_timeout_seconds: int = DEFAULT_STATEMENT_TIMEOUT_SECONDS):
        """
        Initialize database connection.

        Args:
            connection_string: SQLAlchemy connection URL
            max_rows: Maximum rows to return per query (safety limit)
            statement_timeout_seconds: best-effort cap on a single query's run time
        """
        self.connection_string = connection_string
        self.max_rows = max_rows
        self.statement_timeout_seconds = statement_timeout_seconds
        self.engine: Optional[Engine] = None
        self.metadata: Optional[MetaData] = None
        self.schema_cache: Dict[str, Any] = {}
        self.last_schema_refresh: Optional[datetime] = None

    def connect(self) -> None:
        """Establish a read-only database connection and load schema."""
        try:
            effective_url = self.connection_string
            if effective_url.startswith("sqlite:///"):
                effective_url = build_readonly_sqlite_url(effective_url)
                logger.info("SQLite connection opened read-only (mode=ro)")
            else:
                logger.warning(
                    "Non-SQLite dialect: this adapter cannot enforce read-only access in code. "
                    "DATABASE_URL must point at a database user with SELECT-only privileges."
                )

            self.engine = create_engine(
                effective_url,
                pool_pre_ping=True,  # Verify connections before use
                echo=False,
            )

            # Test connection. Dialect-aware: Oracle has no implicit no-table
            # SELECT (ORA-00923) and requires the DUAL pseudo-table.
            connectivity_sql = "SELECT 1 FROM DUAL" if self.engine.dialect.name == "oracle" else "SELECT 1"
            with self.engine.connect() as conn:
                conn.execute(text(connectivity_sql))

            logger.info(f"Connected to database: {self.engine.dialect.name}")

            # Load schema
            self.refresh_schema()

        except SQLAlchemyError as e:
            logger.error("Database connection failed", exc_info=True)
            raise

    def refresh_schema(self) -> None:
        """Scan and cache complete database schema."""
        if not self.engine:
            raise RuntimeError("Database not connected")

        logger.info("Refreshing database schema...")

        # Reflect all tables
        self.metadata = MetaData()
        self.metadata.reflect(bind=self.engine)

        # Build schema cache
        inspector = inspect(self.engine)
        known_tables = inspector.get_table_names()
        self.schema_cache = {
            "tables": {},
            "relationships": [],
            "database_type": self.engine.dialect.name,
        }

        # Extract table details
        for table_name in known_tables:
            columns = []

            for col in inspector.get_columns(table_name):
                columns.append({
                    "name": col["name"],
                    "type": str(col["type"]),
                    "nullable": col.get("nullable", True),
                    "primary_key": col.get("primary_key", False),
                    "default": str(col.get("default", "")),
                })

            # Get foreign keys
            foreign_keys = inspector.get_foreign_keys(table_name)

            # Get sample data (first 3 rows)
            sample_data = self._get_sample_data(table_name, known_tables, limit=3)

            self.schema_cache["tables"][table_name] = {
                "columns": columns,
                "foreign_keys": foreign_keys,
                "sample_data": sample_data,
                "row_count": self._get_row_count(table_name, known_tables),
            }

        self.last_schema_refresh = datetime.now()
        logger.info(f"Schema loaded: {len(self.schema_cache['tables'])} tables")

    def quote_identifier(self, name: str) -> str:
        """
        Dialect-correct identifier quoting. Deliberately NOT a hand-rolled
        f'"{name}"' - SQLAlchemy's reflection normalizes case-insensitive
        identifiers to lowercase on the Python side (Oracle and PostgreSQL
        both store unquoted identifiers in uppercase/lowercase internally
        and fold case on comparison). Force-quoting such a name with literal
        double quotes makes the database require an exact-case match that
        doesn't exist (e.g. a real Oracle table physically named RANK_M is
        reflected here as "rank_m"; `SELECT * FROM "rank_m"` then fails with
        ORA-00942 because quoted identifiers are always case-sensitive).
        The identifier preparer knows, per dialect, whether a name needs
        quoting at all and what case to fold it back to.
        """
        return self.engine.dialect.identifier_preparer.quote(name)

    def _get_sample_data(self, table_name: str, known_tables: List[str], limit: int = 3) -> List[Dict]:
        """Get sample rows from a table. `table_name` must be a member of known_tables (reflected by SQLAlchemy, never user input)."""
        if not _is_known_table_identifier(table_name, known_tables):
            logger.warning("Refused to sample from an unrecognized table identifier")
            return []
        try:
            with self.engine.connect() as conn:
                # Table identifiers cannot be bound as SQL parameters; `table_name` is
                # validated above against the live reflected table list, and the row
                # cap value is bound rather than interpolated. The row-cap SYNTAX is
                # dialect-dependent (e.g. Oracle has no LIMIT keyword at all).
                dialect_name = self.engine.dialect.name
                sample_sql = append_row_limit(dialect_name, f"SELECT * FROM {self.quote_identifier(table_name)}", "limit")
                result = conn.execute(text(sample_sql), {"limit": limit})
                return [dict(row._mapping) for row in result]
        except Exception:
            logger.warning("Could not fetch sample data for a table", exc_info=True)
            return []

    def _get_row_count(self, table_name: str, known_tables: List[str]) -> int:
        """Get approximate row count for a table. `table_name` must be a member of known_tables."""
        if not _is_known_table_identifier(table_name, known_tables):
            logger.warning("Refused to count rows for an unrecognized table identifier")
            return 0
        try:
            with self.engine.connect() as conn:
                result = conn.execute(text(f"SELECT COUNT(*) FROM {self.quote_identifier(table_name)}"))
                return result.scalar() or 0
        except Exception:
            return 0

    def execute_query(self, sql: str) -> Dict[str, Any]:
        """
        Validate and execute a read-only SQL query, returning results or a
        safe error descriptor.

        Returns:
            On success: {"success": True, "columns", "rows", "row_count", "query"}
            On failure: {"success": False, "error_code": one of
                "QUERY_REJECTED" | "QUERY_TIMEOUT" | "DB_UNAVAILABLE" | "INTERNAL",
                "detail": short safe string (no SQL/table/column names)}
        """
        if not self.engine:
            return {"success": False, "error_code": "DB_UNAVAILABLE", "detail": "no active database connection"}

        is_valid, reason = validate_select_only(sql)
        if not is_valid:
            logger.warning("Rejected generated SQL: %s", reason)
            return {"success": False, "error_code": "QUERY_REJECTED", "detail": reason}

        # Append the row cap as a bound parameter rather than interpolating it.
        # The syntax is dialect-dependent (e.g. Oracle has no LIMIT keyword).
        dialect_name = self.engine.dialect.name
        sql_upper = sql.upper()
        bind_params: Dict[str, Any] = {}
        exec_sql = sql.rstrip().rstrip(";")
        if not has_row_limit_clause(dialect_name, sql_upper):
            exec_sql = append_row_limit(dialect_name, exec_sql, "_max_rows")
            bind_params["_max_rows"] = self.max_rows

        try:
            result_holder: Dict[str, Any] = {}
            error_holder: Dict[str, BaseException] = {}

            def _run() -> None:
                try:
                    with self.engine.connect() as conn:
                        result = conn.execute(text(exec_sql), bind_params)
                        rows = result.fetchall()
                        columns = list(result.keys()) if rows else []
                        result_holder["columns"] = columns
                        result_holder["rows"] = [dict(zip(columns, row)) for row in rows]
                except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread
                    error_holder["error"] = exc

            worker = threading.Thread(target=_run, daemon=True)
            worker.start()
            worker.join(timeout=self.statement_timeout_seconds)

            if worker.is_alive():
                # Best-effort cancellation; the thread is left to finish in the
                # background (daemon) since DBAPI connections vary in whether
                # they support safe cross-thread interruption.
                logger.warning("Query exceeded statement timeout of %ss", self.statement_timeout_seconds)
                return {"success": False, "error_code": "QUERY_TIMEOUT", "detail": "statement timeout exceeded"}

            if "error" in error_holder:
                raise error_holder["error"]

            return {
                "success": True,
                "columns": result_holder.get("columns", []),
                "rows": result_holder.get("rows", []),
                "row_count": len(result_holder.get("rows", [])),
                "query": exec_sql,
            }

        except SQLAlchemyError:
            logger.error("Query execution failed", exc_info=True)
            return {"success": False, "error_code": "DB_UNAVAILABLE", "detail": "query execution failed"}
        except Exception:
            logger.error("Unexpected error during query execution", exc_info=True)
            return {"success": False, "error_code": "INTERNAL", "detail": "unexpected error during query execution"}

    def get_schema(self) -> Dict[str, Any]:
        """Return cached schema information."""
        return self.schema_cache

    def close(self) -> None:
        """Close database connection."""
        if self.engine:
            self.engine.dispose()
            logger.info("Database connection closed")
