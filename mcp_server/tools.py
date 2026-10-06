"""
MCP Tool & Resource Definitions (database mode)
Defines FastMCP tools for database querying and schema/example resources.

Hardening notes:
  - query_database (the NL-to-SQL tool) keeps its side effect (it executes a
    real, read-only query) and stays a tool. It is hardened in two
    independent layers: nlp.query_parser.QueryParser._is_safe_query() (a
    keyword blacklist, run on the LLM's raw output) and
    db.adapter.validate_select_only() (a sqlparse-based single-SELECT check,
    run immediately before execution). Either layer failing rejects the
    query - neither is a substitute for the other.
  - get_database_schema and get_query_examples have no side effects and
    only return already-generated data, so they are now resources
    (schema://database, examples://queries) instead of tools.
  - Every handler is wrapped with errors.guarded() so no exception, SQL
    text, table/column name, or stack trace can leak into a response;
    everything funnels through errors.mcp_error() with one of the fixed
    error codes.
"""

import json
from typing import Annotated, Any, Dict

from pydantic import Field

from errors import ErrorCode, guarded, mcp_error
from utils.security import ResponseSanitizer, create_user_friendly_response

MAX_NL_QUESTION_CHARS = 2000

_PARSE_ERROR_CODE = {
    "LLM_UNAVAILABLE": ErrorCode.LLM_UNAVAILABLE,
    "QUERY_REJECTED": ErrorCode.QUERY_REJECTED,
    "INTERNAL": ErrorCode.INTERNAL,
}

_EXEC_ERROR_CODE = {
    "QUERY_REJECTED": ErrorCode.QUERY_REJECTED,
    "QUERY_TIMEOUT": ErrorCode.QUERY_TIMEOUT,
    "DB_UNAVAILABLE": ErrorCode.DB_UNAVAILABLE,
    "INTERNAL": ErrorCode.INTERNAL,
}


def register_tools(mcp, db_adapter, query_parser, config: Dict[str, Any]) -> None:
    """
    Register all database-mode MCP tools and resources with the server.

    Args:
        mcp: FastMCP server instance
        db_adapter: DatabaseAdapter instance
        query_parser: QueryParser instance
        config: Server configuration
    """

    sanitizer = ResponseSanitizer(config)

    @mcp.tool()
    @guarded()
    def query_database(
        natural_language: Annotated[
            str,
            Field(description="A question about the data, in plain English (or Arabic/other supported languages), 1-2000 chars. Example: \"What is the total revenue by product category?\""),
        ]
    ) -> str:
        """
        Convert a natural language question into a read-only SQL query and execute it.
        Use when: you need actual data rows, counts, sums, or other computed results from the database.
        Do not use when: you only need to know which tables/concepts exist (use search_concepts / concept://{id} instead).
        Parameters:
        - natural_language (str): the question, 1-2000 chars. Example: "Show me the top 5 customers by order count".
        Returns: JSON string. Success: {"success": true, "message": str, "data": [object], "count": int}. Failure is raised as a tool error, not returned in this shape.
        Limits: only single SELECT (or WITH...SELECT) statements are ever executed; results capped at a configurable row limit; a statement timeout aborts slow queries.
        """
        stripped = natural_language.strip()
        if not (1 <= len(stripped) <= MAX_NL_QUESTION_CHARS):
            raise mcp_error(
                ErrorCode.INVALID_INPUT,
                f"parameter 'natural_language' must be 1-{MAX_NL_QUESTION_CHARS} chars after stripping",
                next_action=f"pass a 'natural_language' string between 1 and {MAX_NL_QUESTION_CHARS} characters",
            )

        parse_result = query_parser.parse(stripped)

        if not parse_result.get("success"):
            code = _PARSE_ERROR_CODE.get(parse_result.get("error_code"), ErrorCode.INTERNAL)
            raise mcp_error(code, "could not generate a safe SQL query for this question")

        sql = parse_result["sql"]
        query_result = db_adapter.execute_query(sql)

        if not query_result.get("success"):
            code = _EXEC_ERROR_CODE.get(query_result.get("error_code"), ErrorCode.INTERNAL)
            raise mcp_error(code, "the query could not be executed")

        if sanitizer.hide_db_details:
            response = create_user_friendly_response(
                query_result["rows"],
                query_result["row_count"],
                stripped,
            )
        else:
            response = {
                "success": True,
                "natural_language": stripped,
                "sql": sql,
                "columns": query_result["columns"],
                "rows": query_result["rows"],
                "row_count": query_result["row_count"],
            }

        sanitized = sanitizer.sanitize_success(response)
        return json.dumps(sanitized, indent=2, default=str)

    @mcp.tool()
    @guarded()
    def refresh_database_schema() -> str:
        """
        Re-scan the connected database's tables/columns and refresh the cached schema used by query_database.
        Use when: tables or columns changed since the server started and query_database seems out of date.
        Do not use when: nothing has changed in the database structure - this re-reflects every table and is not free.
        Parameters: none.
        Returns: JSON string {"success": true, "message": str, "table_count": int, "timestamp": str}.
        Limits: safe to call repeatedly; does not modify any data.
        """
        db_adapter.refresh_schema()
        query_parser.update_schema(db_adapter.get_schema())

        table_count = len(db_adapter.schema_cache.get("tables", {}))

        return json.dumps({
            "success": True,
            "message": f"Schema refreshed successfully. Found {table_count} tables.",
            "table_count": table_count,
            "timestamp": str(db_adapter.last_schema_refresh),
        }, indent=2)

    @mcp.resource("schema://database")
    @guarded(resource=True)
    def database_schema_resource() -> dict:
        """
        Cached database schema (tables, columns, foreign keys, sample rows) built at last refresh.
        Use when: you need the literal table/column structure, e.g. to debug query_database results.
        Do not use when: you want a conceptual/business explanation of the data (use search_concepts/concept://{id} instead) - this resource may return a generic "not available" message when table/column exposure is disabled by configuration.
        Returns: {"database_type": str, "tables": object} or, when hidden by configuration, {"success": false, "message": str}.
        Limits: reflects the schema as of the last refresh_database_schema call, not necessarily the live structure.
        """
        schema = db_adapter.get_schema()
        return sanitizer.sanitize_schema(schema)

    @mcp.resource("examples://queries")
    @guarded(resource=True)
    def query_examples_resource() -> dict:
        """
        Example natural language questions query_database supports, grouped by category.
        Use when: you want inspiration for how to phrase a question for query_database.
        Do not use when: you need real data - these are static illustrative examples, not live results.
        Returns: {"examples": object, "note": str}.
        Limits: examples are generic and may not match this database's actual schema.
        """
        examples = {
            "basic": [
                "Show me all customers",
                "List all products",
                "What are all the orders?",
            ],
            "filtering": [
                "Show me customers from USA",
                "List products in the Electronics category",
                "What orders were placed in October 2023?",
            ],
            "aggregation": [
                "What is the total number of customers?",
                "How many products do we have in each category?",
                "What is the average order value?",
                "Show me total sales per customer",
            ],
            "sorting_and_top": [
                "Show me the top 5 most expensive products",
                "List customers by registration date, newest first",
                "What are the top 3 customers by order count?",
            ],
            "joins": [
                "Show me all orders with customer names",
                "List all orders with product details",
                "What products have been ordered?",
            ],
            "complex": [
                "What are the products with the highest sales?",
                "Show me customers who haven't placed any orders",
                "What is the revenue by product category?",
                "List inactive customers",
            ],
            "dates": [
                "What orders were placed last month?",
                "Show me customers registered in 2023",
                "What is the monthly order trend?",
            ],
        }

        return {
            "examples": examples,
            "note": "Adapt these examples to match your actual database schema",
        }
