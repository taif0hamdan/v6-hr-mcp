"""
MCP Tool Definitions for API mode.

Per explicit product decision: in API mode, the "data link" tools
(call_api, get_api_schema, refresh_api_spec) all return or act on data
that only exists because of the connected external API. They stay tools,
not resources, even though get_api_schema/refresh_api_spec have no side
effects on our own state - unlike the database-mode schema/examples data,
this is intentionally NOT converted to resources.

All three are still hardened the same way as the rest of the server:
wrapped in errors.guarded(), never leak stack traces/URLs/keys, and use the
shared ErrorCode vocabulary.
"""

import json
from typing import Annotated, Any, Dict

from pydantic import Field

from errors import ErrorCode, guarded, mcp_error
from utils.security import ResponseSanitizer

MAX_NL_REQUEST_CHARS = 2000

_PARSE_ERROR_CODE = {
    "LLM_UNAVAILABLE": ErrorCode.LLM_UNAVAILABLE,
    "QUERY_REJECTED": ErrorCode.QUERY_REJECTED,
    "INTERNAL": ErrorCode.INTERNAL,
}


def register_api_tools(mcp, api_adapter, request_parser, config: Dict[str, Any]) -> None:
    """Register API-mode tools with the server."""

    sanitizer = ResponseSanitizer(config)

    @mcp.tool()
    @guarded()
    def call_api(
        natural_language: Annotated[
            str,
            Field(description="A request in plain English, 1-2000 chars. Example: \"Get user 123\"."),
        ]
    ) -> str:
        """
        Convert a natural language request into an HTTP call against the configured API and execute it.
        Use when: you need live data or an action from the connected external API.
        Do not use when: you only need to know which endpoints exist (use get_api_schema instead).
        Parameters:
        - natural_language (str): the request, 1-2000 chars. Example: "List the 5 most recent orders".
        Returns: JSON string {"success": bool, "data": object|null, "status": int, "method": str, "url": str}.
        Limits: non-GET methods (POST/PUT/DELETE) are rejected unless api.unsafe_mode is enabled; request timeout is 30s.
        """
        stripped = natural_language.strip()
        if not (1 <= len(stripped) <= MAX_NL_REQUEST_CHARS):
            raise mcp_error(
                ErrorCode.INVALID_INPUT,
                f"parameter 'natural_language' must be 1-{MAX_NL_REQUEST_CHARS} chars after stripping",
                next_action=f"pass a 'natural_language' string between 1 and {MAX_NL_REQUEST_CHARS} characters",
            )

        parse_result = request_parser.parse(stripped)

        if not parse_result.get("success"):
            code = _PARSE_ERROR_CODE.get(parse_result.get("error_code"), ErrorCode.INTERNAL)
            raise mcp_error(code, "could not generate a valid API request for this question")

        result = api_adapter.execute_request(
            method=parse_result["method"],
            endpoint=parse_result["endpoint"],
            params=parse_result["params"],
            body=parse_result["body"],
        )

        if not result.get("success"):
            raise mcp_error(ErrorCode.DB_UNAVAILABLE, "the upstream API request failed")

        response = {
            "success": True,
            "data": result.get("data"),
            "status": result.get("status_code"),
            "method": result.get("method"),
            "url": result.get("url"),
        }

        sanitized = sanitizer.sanitize_success(response)
        return json.dumps(sanitized, indent=2, default=str)

    @mcp.tool()
    @guarded()
    def get_api_schema() -> str:
        """
        List the endpoints available on the connected API.
        Use when: you need to know which paths/methods exist before calling call_api.
        Do not use when: you need live data (use call_api instead).
        Parameters: none.
        Returns: JSON string {"info": object, "paths": [str]}.
        Limits: reflects the spec as of the last load/refresh_api_spec call.
        """
        schema = api_adapter.get_schema()
        summary = {
            "info": schema.get("info"),
            "paths": list(schema.get("paths", {}).keys()),
        }
        return json.dumps(summary, indent=2)

    @mcp.tool()
    @guarded()
    def refresh_api_spec() -> str:
        """
        Reload the OpenAPI specification from its configured source.
        Use when: the API's endpoints changed since the server started.
        Do not use when: nothing has changed - this re-fetches/re-parses the whole spec.
        Parameters: none.
        Returns: JSON string {"success": true, "message": str}.
        Limits: if api.spec_source is a remote URL, this requires api.allow_remote_spec to be enabled.
        """
        api_adapter.load_spec()
        return json.dumps({"success": True, "message": "API spec reloaded"})
