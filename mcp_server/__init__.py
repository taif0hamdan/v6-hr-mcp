"""MCP tools module for Smart MCP Server.

Intentionally does not eagerly import submodules here: tools.py/api_tools.py
pull in fastmcp/pydantic, while concepts.py's core logic (ConceptStore) is
designed to stay importable without those installed. Import what you need
directly, e.g. `from mcp_server.tools import register_tools`.
"""

