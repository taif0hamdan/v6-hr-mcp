"""
Centralized error handling for the Smart MCP Server.

Every tool and resource handler must raise errors through the helpers in this
module instead of letting raw exceptions escape. This guarantees:

  - A single, predictable message shape sent to the model/client:
      "Error: <CODE> | <one sentence what went wrong> | Next: <action> | Ref: <id>"
  - Full tracebacks are logged server-side (stderr) only, never returned.
  - No stack traces, exception class names, file paths, table/column names,
    SQL text, connection strings, hostnames, env var values, or API keys ever
    reach the model. Echoed user input is truncated to 50 chars.

Usage:
    from errors import ErrorCode, mcp_error, guarded

    @mcp.tool()
    @guarded
    def my_tool(x: str) -> dict:
        if not x:
            raise mcp_error(ErrorCode.INVALID_INPUT, "parameter 'x' is empty")
        ...
"""

from __future__ import annotations

import functools
import logging
import secrets
import sys
import traceback
from enum import Enum
from typing import Any, Callable, TypeVar

logger = logging.getLogger("errors")

# FastMCP's dedicated exception types surface their message verbatim to the
# model/client while still letting FastMCP mask *other*, unexpected
# exceptions when mask_error_details=True. Fall back to RuntimeError if the
# installed fastmcp version does not expose these (older/newer API drift) so
# the server still runs instead of crashing at import time.
try:
    from fastmcp.exceptions import ToolError, ResourceError
except ImportError:  # pragma: no cover - defensive fallback only
    class ToolError(RuntimeError):
        """Fallback used only if fastmcp.exceptions.ToolError is unavailable."""

    class ResourceError(RuntimeError):
        """Fallback used only if fastmcp.exceptions.ResourceError is unavailable."""


MAX_ECHOED_INPUT = 50


class ErrorCode(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"
    CONCEPT_NOT_FOUND = "CONCEPT_NOT_FOUND"
    DATALINK_NOT_FOUND = "DATALINK_NOT_FOUND"
    NO_MATCH = "NO_MATCH"
    QUERY_REJECTED = "QUERY_REJECTED"
    QUERY_TIMEOUT = "QUERY_TIMEOUT"
    DB_UNAVAILABLE = "DB_UNAVAILABLE"
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"
    INTERNAL = "INTERNAL"


# Required "Next:" action text per code. NO_MATCH is handled separately since
# it is not an error at all (empty result + hint field), but we keep an entry
# here for consistency in case a caller wants the phrasing.
_NEXT_ACTION = {
    ErrorCode.INVALID_INPUT: None,  # caller must supply the specific parameter/range
    ErrorCode.CONCEPT_NOT_FOUND: "call search_concepts with a keyword, or read concepts://index for valid ids",
    ErrorCode.DATALINK_NOT_FOUND: "read datalinks://index for valid ids",
    ErrorCode.NO_MATCH: "try fewer or broader keywords",
    ErrorCode.QUERY_REJECTED: "rephrase as a read-only question; only single SELECT statements are allowed",
    ErrorCode.QUERY_TIMEOUT: "narrow the question (add filters or a smaller time range)",
    ErrorCode.DB_UNAVAILABLE: "retry once; if it persists, tell the user the database is unavailable",
    ErrorCode.LLM_UNAVAILABLE: "retry once; if it persists, use search_concepts and concept:// instead",
    ErrorCode.INTERNAL: "retry once; if it persists, report the reference id to the user",
}


def new_ref_id() -> str:
    """8 hex-character reference id for correlating a user-facing error with server logs."""
    return secrets.token_hex(4)


def truncate_echo(text: str, limit: int = MAX_ECHOED_INPUT) -> str:
    """Truncate user-supplied text before it is ever echoed back in a message."""
    if text is None:
        return ""
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def build_message(code: ErrorCode, what_happened: str, next_action: str | None = None, ref_id: str | None = None) -> str:
    """
    Build the exact required error string:
      "Error: <CODE> | <what went wrong, one sentence> | Next: <action> | Ref: <id>"
    """
    action = next_action or _NEXT_ACTION.get(code) or "retry once; if it persists, report the reference id to the user"
    ref = ref_id or new_ref_id()
    return f"Error: {code.value} | {what_happened} | Next: {action} | Ref: {ref}"


def mcp_error(code: ErrorCode, what_happened: str, next_action: str | None = None, resource: bool = False) -> Exception:
    """
    Build the appropriate FastMCP exception (ToolError or ResourceError) carrying
    our masked, formatted message. Also logs a correlated server-side record.

    Args:
        code: one of ErrorCode
        what_happened: one sentence, must NOT contain secrets/paths/SQL/table or
            column names/stack traces. Any echoed user input must already be
            truncated via truncate_echo() before being placed here.
        next_action: override the default "Next:" text for this code (rarely needed
            except INVALID_INPUT, which must name the parameter and allowed range).
        resource: True to raise ResourceError instead of ToolError.
    """
    ref_id = new_ref_id()
    message = build_message(code, what_happened, next_action, ref_id)
    logger.error("ref=%s code=%s detail=%s", ref_id, code.value, what_happened)
    exc_cls = ResourceError if resource else ToolError
    return exc_cls(message)


F = TypeVar("F", bound=Callable[..., Any])


def _sanitize_unexpected_exception(exc: BaseException, resource: bool) -> Exception:
    """Convert an unexpected (non-mcp_error) exception into our masked error shape."""
    ref_id = new_ref_id()
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error("ref=%s code=%s UNEXPECTED exception:\n%s", ref_id, ErrorCode.INTERNAL.value, tb_text)
    message = build_message(ErrorCode.INTERNAL, "an unexpected internal error occurred", ref_id=ref_id)
    exc_cls = ResourceError if resource else ToolError
    return exc_cls(message)


def guarded(resource: bool = False) -> Callable[[F], F]:
    """
    Decorator for every tool/resource handler. Catches ALL exceptions, logs the
    full traceback to stderr with a reference id, and converts to the required
    masked message format. A handler that already raises a ToolError/ResourceError
    (via mcp_error) is passed through unchanged (it is already masked and logged).

    Usage:
        @mcp.tool()
        @guarded()
        def my_tool(...): ...

        @mcp.resource("concept://{concept_id}")
        @guarded(resource=True)
        def concept_resource(concept_id: str): ...
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except (ToolError, ResourceError):
                # Already a deliberately-raised, masked error. Re-raise as-is.
                raise
            except Exception as exc:  # noqa: BLE001 - this is the catch-all boundary
                raise _sanitize_unexpected_exception(exc, resource) from None

        return wrapper  # type: ignore[return-value]

    return decorator


def configure_stderr_logging(level: int = logging.INFO) -> None:
    """Ensure error logs (with full tracebacks) always go to stderr, never stdout."""
    root = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler) and h.stream is sys.stderr for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
