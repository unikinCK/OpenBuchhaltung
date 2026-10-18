"""Aufbereitung von Tool-Aufrufen für die Chat-Oberfläche (app/services/chat_display.py)."""

from __future__ import annotations

import json

from app.services.chat import mark_untrusted, truncate_tool_result
from app.services.chat_display import action_summary, display_result, tool_call_view


def test_action_summary_formats_fields_and_line_table():
    summary = action_summary(
        {
            "company_id": 1,
            "entry_date": "2026-10-15",
            "description": "Büromaterial",
            "reference": "",
            "lines": [
                {"account_code": "4930", "debit_amount": "1234.5", "tax_code_id": 3},
                {"account_code": "1000", "credit_amount": "1234.50", "line_number": None},
            ],
        }
    )
    assert summary["fields"] == [["Buchungsdatum", "15.10.2026"], ["Text", "Büromaterial"]]
    [table] = summary["tables"]
    assert table["label"] == "Buchungszeilen"
    assert table["columns"] == ["Konto", "Soll", "Steuercode (ID)", "Haben"]
    assert table["numeric"] == [False, True, False, True]
    assert table["rows"] == [["4930", "1.234,50", "3", ""], ["1000", "", "", "1.234,50"]]


def test_action_summary_hides_file_content_and_maps_booleans():
    summary = action_summary(
        {"file_name": "a.pdf", "content_base64": "QUJD", "is_active": False, "client_sha256": "x"}
    )
    assert summary["fields"] == [
        ["Datei", "a.pdf"],
        ["Aktiv", "Nein"],
        ["Datei", "wird mit hochgeladen"],
    ]


def test_display_result_strips_untrusted_marker_and_indents_json():
    text = mark_untrusted("Tool-Ergebnis list_accounts", json.dumps({"items": [1]}))
    assert display_result(text) == '{\n  "items": [\n    1\n  ]\n}'
    # Beim Speichern gekürzt: Ende-Markierung fehlt, Anfang wird trotzdem entfernt.
    truncated = mark_untrusted("Tool-Ergebnis x", "abc")[:-10] + "\n… [gekürzt]"
    assert not display_result(truncated).startswith("[Beginn")
    assert display_result("Fehler: nicht gefunden") == "Fehler: nicht gefunden"


def test_display_result_indents_truncated_compact_json():
    accounts = {"accounts": [{"code": f"{n:04d}", "name": "a, b {x}"} for n in range(50)]}
    compact = json.dumps(accounts, ensure_ascii=False, separators=(",", ":"))
    stored = truncate_tool_result(mark_untrusted("Tool-Ergebnis list_accounts", compact), 300)
    shown = display_result(stored)
    assert shown.startswith('{\n  "accounts": [\n    {\n      "code": "0000",\n')
    # Kommas und Klammern in Strings bleiben unangetastet.
    assert '"name": "a, b {x}"' in shown
    assert shown.endswith("\n… (gekürzt)")
    assert "[Beginn" not in shown


def test_tool_call_view_adds_title_without_touching_stored_call():
    call = {"name": "list_documents", "arguments": {}, "result_text": "[]"}
    view = tool_call_view(call)
    assert view["title"] == "Belege durchsuchen"
    assert view["result_display"] == "[]"
    assert "title" not in call
