"""
Concepts: "search then read" over database_docs/.

A "concept" is one documentation file in the docs directory (default
database_docs/, overridable via DOCS_DIR). At startup every concept doc is
loaded into an in-memory SQLite database with an FTS5 full-text index, using
only the stdlib `sqlite3` module (no extra dependency).

Exposed to the model:
  - Tool `search_concepts(query, limit)`      -> ids + snippets only, never full bodies.
  - Resource `concepts://index`                -> static {id, title} list, built once at startup.
  - Resource `concept://{concept_id}`           -> full doc for one id.

Query text for FTS5 is never built with string formatting: it is tokenized
with `re.findall(r"\\w+", query)`, each token individually double-quoted, and
the resulting MATCH expression is passed as a single bound parameter.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

# `from __future__ import annotations` (above) makes every annotation in this
# file a deferred string, evaluated later against this module's globals -
# notably by pydantic, when FastMCP inspects the tool/resource function
# signatures below to build their schemas. Field must therefore live at
# MODULE scope (not be a local import inside register_concept_tools()) or
# that later evaluation fails with NameError: name 'Field' is not defined.
# The try/except keeps ConceptStore / discover_concepts / CONCEPT_ID_RE
# importable even where pydantic isn't installed (e.g. the lighter-weight
# test environment for test_security.py) - only register_concept_tools()
# actually requires it, and it will fail loudly and obviously if called
# without pydantic present.
try:
    from pydantic import Field
except ImportError:  # pragma: no cover - exercised only without pydantic installed
    Field = None

from errors import ErrorCode, guarded, mcp_error, truncate_echo

logger = logging.getLogger("concepts")

CONCEPT_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")
DOC_EXTENSIONS = (".yaml", ".yml")
MAX_SNIPPET_CHARS = 200


@dataclass(frozen=True)
class Concept:
    id: str
    title: str
    body: str


def _derive_title(stem: str, parsed: Any) -> str:
    """Best-effort human title for a concept doc: domain > description > filename."""
    if isinstance(parsed, dict):
        ctx = parsed.get("database_context", parsed)
        if isinstance(ctx, dict):
            domain = ctx.get("domain")
            if isinstance(domain, str) and domain.strip():
                return domain.strip()
            description = ctx.get("description")
            if isinstance(description, str) and description.strip():
                return description.strip()[:120]
    return stem.replace("_", " ").replace("-", " ").title()


def discover_concepts(docs_dir: Path) -> list[Concept]:
    """
    Scan docs_dir for concept documents (*.yaml / *.yml only — README.md and
    other human-guide files are intentionally excluded by extension, not by
    hardcoded name, so new concept docs just need to be dropped in the
    directory to be picked up).

    A file whose derived id fails validation is skipped with a warning, never
    crashes startup.
    """
    import yaml  # local import: keep this module importable even if pyyaml is absent elsewhere

    concepts: list[Concept] = []
    if not docs_dir.is_dir():
        logger.warning("Docs directory does not exist, concepts index will be empty")
        return concepts

    for path in sorted(docs_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in DOC_EXTENSIONS:
            continue

        concept_id = path.stem.lower()
        if not CONCEPT_ID_RE.match(concept_id):
            logger.warning("Skipping concept doc with invalid id (name does not match slug rules)")
            continue

        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError:
            logger.warning("Skipping concept doc that could not be read: id=%s", concept_id)
            continue

        try:
            parsed = yaml.safe_load(raw_text)
        except yaml.YAMLError:
            parsed = None
            logger.warning("Concept doc id=%s is not valid YAML; indexing as plain text", concept_id)

        title = _derive_title(concept_id, parsed)
        concepts.append(Concept(id=concept_id, title=title, body=raw_text))

    return concepts


class ConceptStore:
    """In-memory FTS5-backed store of concept docs, built once at startup."""

    def __init__(self, docs_dir: str | Path):
        self.docs_dir = Path(docs_dir)
        self._conn: sqlite3.Connection | None = None
        self._index: list[dict[str, str]] = []

    def load(self) -> None:
        concepts = discover_concepts(self.docs_dir)

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE concepts_fts USING fts5(id, title, body)")
        conn.executemany(
            "INSERT INTO concepts_fts (id, title, body) VALUES (?, ?, ?)",
            [(c.id, c.title, c.body) for c in concepts],
        )
        conn.commit()

        self._conn = conn
        self._index = sorted(
            ({"id": c.id, "title": c.title} for c in concepts),
            key=lambda item: item["id"],
        )
        logger.info("Loaded %d concept doc(s) from %s", len(concepts), self.docs_dir)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def index(self) -> list[dict[str, str]]:
        return self._index

    def _build_match_expression(self, query: str) -> str | None:
        tokens = re.findall(r"\w+", query)
        if not tokens:
            return None
        return " ".join(f'"{tok}"' for tok in tokens)

    def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        assert self._conn is not None, "ConceptStore.load() must be called first"

        match_expr = self._build_match_expression(query)
        if match_expr is None:
            raise mcp_error(
                ErrorCode.INVALID_INPUT,
                "parameter 'query' contains no searchable keywords (letters/digits/underscore)",
                next_action="pass at least one word or number in 'query' (1-200 chars)",
            )

        cursor = self._conn.execute(
            """
            SELECT id, title, snippet(concepts_fts, 2, '', '', '...', 10) AS snip, bm25(concepts_fts) AS score
            FROM concepts_fts
            WHERE concepts_fts MATCH ?
            ORDER BY bm25(concepts_fts)
            LIMIT ?
            """,
            (match_expr, limit),
        )
        results = []
        for row_id, title, snippet, score in cursor.fetchall():
            snippet = (snippet or "").strip()
            if len(snippet) > MAX_SNIPPET_CHARS:
                snippet = snippet[:MAX_SNIPPET_CHARS].rstrip() + "..."
            results.append({"id": row_id, "title": title, "snippet": snippet, "score": float(score)})
        return results

    def get(self, concept_id: str) -> str | None:
        assert self._conn is not None, "ConceptStore.load() must be called first"
        cursor = self._conn.execute(
            "SELECT body FROM concepts_fts WHERE id = ? LIMIT 1", (concept_id,)
        )
        row = cursor.fetchone()
        return row[0] if row else None


def register_concept_tools(mcp, docs_dir: str | Path) -> ConceptStore:
    """Load the concept store and register search_concepts tool + concept(s) resources."""
    if Field is None:
        raise ImportError("pydantic is required to register concept tools/resources (ConceptStore itself does not need it)")

    store = ConceptStore(docs_dir)
    store.load()

    @mcp.tool()
    @guarded()
    def search_concepts(
        query: Annotated[
            str,
            Field(description="Keywords to search for, 1-200 chars after stripping. Punctuation is ignored, only word/number tokens are matched. Example: \"anime rating studio\"."),
        ],
        limit: Annotated[
            int,
            Field(description="Maximum number of results, 1-20.", ge=1, le=20),
        ] = 5,
    ) -> dict:
        """
        Search database documentation (concepts) by keyword. Returns ids, not full documents.
        Use when: you need to know which tables/entities relate to a user question, before running a data query.
        Do not use when: you already have a concept id (read concept://{id} instead), or you need actual data rows (use the SQL question tool).
        Parameters:
        - query (str): keywords, 1-200 chars. Punctuation is ignored. Example: "customer orders revenue".
        - limit (int): max results, 1-20, default 5.
        Returns: {"results": [{"id": str, "title": str, "snippet": str, "score": float}], "count": int, "hint": str|null}
        Limits: snippets <=200 chars; results ranked by relevance (lower score = more relevant); empty results are not an error.
        """
        stripped = query.strip()
        if not (1 <= len(stripped) <= 200):
            raise mcp_error(
                ErrorCode.INVALID_INPUT,
                f"parameter 'query' must be 1-200 chars after stripping, got '{truncate_echo(stripped)}'",
                next_action="pass a 'query' string between 1 and 200 characters",
            )
        if not (1 <= limit <= 20):
            raise mcp_error(
                ErrorCode.INVALID_INPUT,
                f"parameter 'limit' must be between 1 and 20, got {limit}",
                next_action="pass a 'limit' integer between 1 and 20",
            )

        results = store.search(stripped, limit)
        if not results:
            return {"results": [], "count": 0, "hint": "try fewer or broader keywords"}
        return {"results": results, "count": len(results), "hint": None}

    @mcp.resource("concepts://index")
    @guarded(resource=True)
    def concepts_index() -> dict:
        """
        Static list of all known concept ids and titles, built once at startup.
        Use when: you want to browse all available concepts instead of searching by keyword.
        Returns: {"concepts": [{"id": str, "title": str}], "count": int}
        Limits: read-only, does not reflect docs added after server startup.
        """
        return {"concepts": store.index, "count": len(store.index)}

    @mcp.resource("concept://{concept_id}")
    @guarded(resource=True)
    def concept_resource(concept_id: str) -> dict:
        """
        Full documentation body for one concept id.
        Use when: search_concepts returned this id as the single best match for the user's question.
        Do not use when: you have not called search_concepts yet, or need more than one concept (read the single best match first).
        Parameters:
        - concept_id (str): must match ^[a-z0-9_-]{1,64}$. Get valid ids from search_concepts or concepts://index.
        Returns: {"id": str, "title": str, "body": str}
        Limits: unknown but well-formed ids return CONCEPT_NOT_FOUND; malformed ids return INVALID_INPUT.
        """
        if not CONCEPT_ID_RE.match(concept_id):
            raise mcp_error(
                ErrorCode.INVALID_INPUT,
                f"parameter 'concept_id' must match ^[a-z0-9_-]{{1,64}}$, got '{truncate_echo(concept_id)}'",
                next_action="pass a concept_id of 1-64 lowercase letters, digits, '_' or '-' only",
                resource=True,
            )

        body = store.get(concept_id)
        if body is None:
            raise mcp_error(
                ErrorCode.CONCEPT_NOT_FOUND,
                f"no concept exists with id '{truncate_echo(concept_id)}'",
                resource=True,
            )

        title = next((c["title"] for c in store.index if c["id"] == concept_id), concept_id)
        return {"id": concept_id, "title": title, "body": body}

    return store
