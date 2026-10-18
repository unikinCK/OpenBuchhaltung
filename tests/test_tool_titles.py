"""Deutsche Anzeigenamen der Tools (MCP ``title``, KI-Chat)."""

from __future__ import annotations

from app.services.mcp_server import TOOLS
from app.services.tool_titles import TOOL_TITLES, tool_title


def test_every_tool_has_exactly_one_title():
    names = {tool.name for tool in TOOLS}
    assert names - set(TOOL_TITLES) == set(), "Titel fehlt"
    assert set(TOOL_TITLES) - names == set(), "Titel ohne Tool"
    assert all(title.strip() and len(title) <= 60 for title in TOOL_TITLES.values())


def test_mcp_tool_listing_carries_title():
    tool = next(tool for tool in TOOLS if tool.name == "list_journal_entries")
    assert tool.to_mcp()["title"] == "Buchungen durchsuchen"


def test_unknown_tool_gets_readable_fallback():
    assert tool_title("do_something_new") == "Do something new"
