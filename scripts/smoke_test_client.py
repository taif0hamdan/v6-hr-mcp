"""
Quick end-to-end smoke test for a running Smart MCP Server, using fastmcp's
own Client (no OpenWebUI, no browser - this is a JSON-RPC API, not a web
page, so a browser tab was never going to show anything useful).

Usage:
    .venv/bin/python scripts/smoke_test_client.py [url] [--query-db "question"]

Examples:
    # Just prove the server/tools/concepts work (no LLM call):
    .venv/bin/python scripts/smoke_test_client.py http://localhost:1311/mcp/

    # Also exercise the full NL-to-SQL pipeline (needs the LLM reachable):
    .venv/bin/python scripts/smoke_test_client.py http://localhost:1311/mcp/ \
        --query-db "How many departments are there?"
"""

import asyncio
import sys

from fastmcp import Client


async def main(url: str, nl_question: str | None) -> None:
    print(f"Connecting to {url} ...")
    async with Client(url) as client:
        print("Connected. Handshake OK.\n")

        tools = await client.list_tools()
        print(f"Tools ({len(tools)}):")
        for t in tools:
            print(f"  - {t.name}")

        resources = await client.list_resources()
        resource_templates = await client.list_resource_templates()
        print(f"\nResources ({len(resources)} static, {len(resource_templates)} templated):")
        for r in resources:
            print(f"  - {r.uri}")
        for rt in resource_templates:
            print(f"  - {rt.uriTemplate}")

        print("\n--- Reading concepts://index ---")
        index = await client.read_resource("concepts://index")
        print(index[0].text if index else "(empty)")

        print("\n--- Calling search_concepts(query='anime rating') ---")
        result = await client.call_tool("search_concepts", {"query": "anime rating", "limit": 3})
        print(result[0].text if result else "(empty)")

        if nl_question:
            print(f"\n--- Calling query_database(natural_language={nl_question!r}) ---")
            print("(this calls the configured LLM - may take a moment, or fail with LLM_UNAVAILABLE if it's unreachable from here)")
            result = await client.call_tool("query_database", {"natural_language": nl_question})
            print(result[0].text if result else "(empty)")

    print("\n✅ Done - server, tools, and concepts search are working.")


if __name__ == "__main__":
    args = sys.argv[1:]
    url = "http://localhost:1311/mcp/"
    nl_question = None

    if args and not args[0].startswith("--"):
        url = args.pop(0)
    if args and args[0] == "--query-db":
        nl_question = args[1] if len(args) > 1 else None

    asyncio.run(main(url, nl_question))
