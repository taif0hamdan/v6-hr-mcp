"""
Natural Language to SQL Query Parser
Uses LLM to convert natural language questions into safe SQL queries.
"""

from typing import Dict, Any
import logging
import re

from openai import APIConnectionError, APIError, APITimeoutError

from utils.llm_client import build_extra_body, build_openai_client, disable_thinking_suffix, resolve_llm_settings
from utils.schema import format_schema_for_llm, format_examples_for_llm, get_safety_rules

logger = logging.getLogger(__name__)


class QueryParser:
    """Converts natural language to SQL using LLM with schema context."""

    def __init__(self, llm_config: Dict[str, Any], schema: Dict[str, Any]):
        """
        Initialize query parser with LLM configuration.

        Args:
            llm_config: LLM settings from config.yaml
            schema: Database schema from DatabaseAdapter
        """
        self.llm_config = llm_config
        self.schema = schema

        settings = resolve_llm_settings(llm_config)
        self.client = build_openai_client(settings)
        self.model = settings["model"]
        self.temperature = settings["temperature"]
        self.max_tokens = settings["max_tokens"]
        self.timeout_seconds = settings["timeout_seconds"]
        self.extra_body = build_extra_body(settings) or None
        self.no_think_suffix = disable_thinking_suffix(settings)

        logger.info(
            "Query parser initialized: model=%s timeout=%ss num_ctx=%s disable_thinking=%s",
            self.model, self.timeout_seconds, settings.get("num_ctx"), settings.get("disable_thinking"),
        )

    def parse(self, natural_language: str) -> Dict[str, Any]:
        """
        Convert natural language to SQL query.

        Args:
            natural_language: User's question in plain English

        Returns:
            Dict with 'success' and either 'sql', or 'error_code' + 'error'.
            error_code is one of "LLM_UNAVAILABLE" | "QUERY_REJECTED" | "INTERNAL".
        """
        try:
            prompt = self._build_prompt(natural_language) + self.no_think_suffix

            create_kwargs = dict(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert SQL query generator. Return ONLY valid SQL queries, no explanations.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            if self.extra_body:
                create_kwargs["extra_body"] = self.extra_body

            response = self.client.chat.completions.create(**create_kwargs)
        except (APIConnectionError, APITimeoutError, APIError) as exc:
            # Logged server-side only (stderr) - never sent to the model/client.
            # The exception TYPE/status here is the single most useful thing
            # for diagnosing *why* the LLM is unreachable (DNS/connect vs.
            # timeout vs. a 404 "model not found" vs. some other 4xx/5xx).
            status_code = getattr(exc, "status_code", None)
            logger.error("LLM call failed: %s (status_code=%s)", type(exc).__name__, status_code, exc_info=True)
            return {"success": False, "error_code": "LLM_UNAVAILABLE", "error": "the configured LLM endpoint is unavailable"}
        except Exception:
            logger.error("Unexpected error calling the LLM", exc_info=True)
            return {"success": False, "error_code": "INTERNAL", "error": "unexpected error while generating SQL"}

        try:
            sql = response.choices[0].message.content.strip()
            sql = self._clean_sql(sql)

            if not self._is_safe_query(sql):
                return {"success": False, "error_code": "QUERY_REJECTED", "error": "generated query failed safety validation"}

            return {"success": True, "sql": sql, "natural_language": natural_language}

        except Exception:
            logger.error("Failed to process LLM response", exc_info=True)
            return {"success": False, "error_code": "INTERNAL", "error": "unexpected error while generating SQL"}

    def _build_prompt(self, natural_language: str) -> str:
        """Build comprehensive prompt with schema, context, and examples."""
        schema_text = format_schema_for_llm(self.schema)
        examples_text = format_examples_for_llm()
        safety_rules = get_safety_rules()

        # Add database context if available (improves query quality!)
        context_text = self._build_database_context()

        prompt = f"""Convert the following natural language question into a SQL query.

{safety_rules}

{context_text}

{schema_text}

{examples_text}

NATURAL LANGUAGE QUESTION:
"{natural_language}"

IMPORTANT: Return ONLY the SQL query, nothing else. No markdown, no explanations.
"""
        return prompt

    def _build_database_context(self) -> str:
        """Build database context section from config (if available)."""
        db_context = self.llm_config.get("database_context", {})

        if not db_context:
            return ""

        context_parts = ["DATABASE CONTEXT:", "=" * 80]

        # Description
        if "description" in db_context:
            context_parts.append(f"Description: {db_context['description']}")

        # Domain
        if "domain" in db_context:
            context_parts.append(f"Domain: {db_context['domain']}")

        # Business concepts
        if "business_concepts" in db_context:
            context_parts.append("\nKey Concepts:")
            for concept in db_context["business_concepts"]:
                context_parts.append(f"  • {concept}")

        # Table documentation
        if "tables" in db_context:
            context_parts.append("\nTable Descriptions:")
            for table_name, table_info in db_context["tables"].items():
                desc = table_info.get("description", "")
                notes = table_info.get("notes", "")
                context_parts.append(f"  • {table_name}: {desc}")
                if notes:
                    context_parts.append(f"    ({notes})")

        # Data conventions
        if "conventions" in db_context:
            context_parts.append("\nData Conventions:")
            for key, value in db_context["conventions"].items():
                context_parts.append(f"  • {key}: {value}")

        # Common queries
        if "common_queries" in db_context:
            context_parts.append("\nCommon Use Cases:")
            for query in db_context["common_queries"][:5]:  # Show first 5
                context_parts.append(f"  • {query}")

        context_parts.append("=" * 80)
        context_parts.append("")

        return "\n".join(context_parts)

    def _clean_sql(self, sql: str) -> str:
        """Clean and normalize SQL query."""
        # Remove markdown code blocks
        sql = re.sub(r'```sql\s*', '', sql)
        sql = re.sub(r'```\s*', '', sql)

        # Remove extra whitespace
        sql = ' '.join(sql.split())

        # Remove trailing semicolon if present
        sql = sql.rstrip(';')

        return sql.strip()

    def _is_safe_query(self, sql: str) -> bool:
        """
        Validate query safety (read-only checks). This is the SECOND layer of
        defense - the first (and authoritative) layer is the sqlparse-based
        single-SELECT-statement check in db.adapter.validate_select_only(),
        applied just before execution.

        Args:
            sql: SQL query string

        Returns:
            True if query is safe, False otherwise
        """
        sql_upper = sql.upper()

        # Check for dangerous commands
        dangerous_keywords = [
            'DROP', 'DELETE', 'UPDATE', 'INSERT', 'ALTER',
            'TRUNCATE', 'CREATE', 'GRANT', 'REVOKE', 'EXEC',
            'EXECUTE', 'SCRIPT', '--', '/*', '*/', 'UNION'
        ]

        for keyword in dangerous_keywords:
            if keyword in sql_upper:
                logger.warning(f"Unsafe keyword detected: {keyword}")
                return False

        # Must start with SELECT
        if not sql_upper.strip().startswith('SELECT'):
            logger.warning("Query does not start with SELECT")
            return False

        # Check for multiple statements (SQL injection attempt)
        if ';' in sql:
            logger.warning("Multiple statements detected")
            return False

        return True

    def update_schema(self, schema: Dict[str, Any]) -> None:
        """Update schema context for parser."""
        self.schema = schema
        logger.info("Query parser schema updated")
