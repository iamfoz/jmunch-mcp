"""Tiny MCP stdio server for the jmunch demo.

Exposes one tool — `get_document` — that returns the full SQLite Wikipedia
article (~400 KB of raw HTML) as a single text response. Single-turn fat
payload guarantees jmunch's handle-ification triggers deterministically
every run, regardless of Claude's tool-loop behavior.

First invocation downloads the page to demo_doc.txt via urllib; subsequent
runs are fully offline. No API keys needed.
"""
from __future__ import annotations

import asyncio
import urllib.request
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

HERE = Path(__file__).parent
DOC_PATH = HERE / "demo_doc.txt"
SOURCE_URL = "https://en.wikipedia.org/wiki/SQLite"


def _ensure_doc() -> str:
    if not DOC_PATH.exists():
        req = urllib.request.Request(
            SOURCE_URL,
            headers={"User-Agent": "jmunch-demo/1.0 (+https://github.com/jgravelle/jmunch-mcp)"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        DOC_PATH.write_text(html, encoding="utf-8")
    return DOC_PATH.read_text(encoding="utf-8")


app = Server("demo-doc")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="get_document",
            description=(
                "Fetch the entire SQLite Wikipedia article as a single blob of HTML/text. "
                "No parameters. Always returns the same document so behaviour is reproducible. "
                "Use this when asked to summarise, quote, or extract facts from the article."
            ),
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        )
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    if name != "get_document":
        raise ValueError(f"unknown tool: {name}")
    return [TextContent(type="text", text=_ensure_doc())]


async def main() -> None:
    async with stdio_server() as (reader, writer):
        await app.run(reader, writer, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
