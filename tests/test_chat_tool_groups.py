"""KI-Chat: Werkzeuge seltener Fachgebiete werden erst auf Anforderung eingeblendet."""

from __future__ import annotations

import json
from types import SimpleNamespace

from app import create_app
from app.services.chat import (
    CHAT_TOOL_GROUPS,
    LOAD_TOOLS_NAME,
    build_system_prompt,
    build_tool_executor,
    chat_available_tools,
    chat_tool_blocked,
    chat_tool_definitions,
    chat_tool_requires_confirmation,
    loaded_tool_groups,
)
from app.services.mcp_server import TOOLS_BY_NAME
from app.services.tool_titles import tool_title


def _names(definitions) -> set[str]:
    return {definition["name"] for definition in definitions}


def test_groups_name_existing_available_tools_once() -> None:
    seen: set[str] = set()
    for _title, names in CHAT_TOOL_GROUPS.values():
        for name in names:
            assert name in TOOLS_BY_NAME, name
            assert not chat_tool_blocked(name), name
            assert name not in seen, name
            seen.add(name)


def test_default_definitions_hide_groups_but_offer_load_tools() -> None:
    names = _names(chat_tool_definitions())
    assert LOAD_TOOLS_NAME in names
    assert "create_payroll_employee" not in names
    assert "create_fixed_asset" not in names
    assert "create_journal_entry" in names
    assert "search_knowledge" in names


def test_loaded_group_adds_its_tools_and_all_groups_cover_every_tool() -> None:
    names = _names(chat_tool_definitions(["lohn"]))
    assert "create_payroll_employee" in names
    assert "create_fixed_asset" not in names

    everything = _names(chat_tool_definitions(CHAT_TOOL_GROUPS))
    assert everything - {LOAD_TOOLS_NAME} == {tool.name for tool in chat_available_tools()}


def test_load_tools_needs_no_confirmation_and_is_not_blocked() -> None:
    assert chat_tool_blocked(LOAD_TOOLS_NAME) is False
    assert chat_tool_requires_confirmation(LOAD_TOOLS_NAME) is False
    assert tool_title(LOAD_TOOLS_NAME) == "Werkzeuge einblenden"


def test_write_tools_of_a_group_stay_confirmation_bound() -> None:
    definitions = {d["name"]: d for d in chat_tool_definitions(["anlagen"])}
    assert "Bestätigung" in definitions["create_fixed_asset"]["description"]
    assert "Bestätigung" not in definitions["list_fixed_assets"]["description"]


def test_executor_extends_the_running_definition_list_once(tmp_path) -> None:
    app = create_app(
        {"TESTING": True, "DATABASE_URL": f"sqlite+pysqlite:///{tmp_path / 'groups.db'}"}
    )
    tools = chat_tool_definitions()
    with app.app_context():
        execute = build_tool_executor(api_user=None, global_access=True, tool_definitions=tools)
    before = len(tools)

    text, is_error = execute(LOAD_TOOLS_NAME, {"group": "anlagen"})
    assert is_error is False
    assert "create_fixed_asset" in text
    assert "create_fixed_asset" in _names(tools)
    grown = len(tools)
    assert grown == before + len(CHAT_TOOL_GROUPS["anlagen"][1])

    # Erneutes Laden fügt nichts doppelt hinzu.
    execute(LOAD_TOOLS_NAME, {"group": "anlagen"})
    assert len(tools) == grown

    text, is_error = execute(LOAD_TOOLS_NAME, {"group": "gibt-es-nicht"})
    assert is_error is True
    assert "lohn" in text
    assert len(tools) == grown


def test_loaded_groups_are_read_from_the_conversation() -> None:
    messages = [
        SimpleNamespace(tool_calls=None),
        SimpleNamespace(
            tool_calls=[
                {"name": "list_accounts", "arguments": {"company_id": 1}},
                {"name": LOAD_TOOLS_NAME, "arguments": {"group": "lohn"}},
                {"name": LOAD_TOOLS_NAME, "arguments": {"group": "lohn"}},
                {"name": LOAD_TOOLS_NAME, "arguments": {"group": "unbekannt"}},
            ]
        ),
        SimpleNamespace(tool_calls=[{"name": LOAD_TOOLS_NAME, "arguments": {"group": "anlagen"}}]),
    ]
    assert loaded_tool_groups(messages) == ["lohn", "anlagen"]


def test_system_prompt_names_the_groups() -> None:
    company = SimpleNamespace(name="Test GmbH", id=1, currency_code="EUR")
    prompt = build_system_prompt(company, username=None, role=None)
    assert "load_tools" in prompt
    for group in CHAT_TOOL_GROUPS:
        assert group in prompt


def test_definitions_are_much_smaller_than_the_full_list() -> None:
    default = len(json.dumps(chat_tool_definitions(), ensure_ascii=False))
    full = len(json.dumps(chat_tool_definitions(CHAT_TOOL_GROUPS), ensure_ascii=False))
    assert default < full * 0.85
