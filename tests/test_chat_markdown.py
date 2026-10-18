"""Markdown-Darstellung im KI-Chat (app/static/chat_markdown.js)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC = PROJECT_ROOT / "app" / "static"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js nicht installiert")
def test_markdown_renderer():
    result = subprocess.run(
        ["node", str(PROJECT_ROOT / "tests" / "js" / "chat_markdown_test.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.parametrize("name", ["chat.js", "chat_markdown.js"])
def test_chat_scripts_never_inject_html(name):
    # KI-Antworten sind nicht vertrauenswürdig: DOM nur per createElement/textContent.
    source = (STATIC / name).read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in source, f"{name} verwendet {sink}"


def test_chat_script_wires_input_events():
    # Regressionsschutz: Enter sendet, Dateiauswahl landet in der Vorschau.
    source = (STATIC / "chat.js").read_text(encoding="utf-8")
    assert 'textarea.addEventListener("keydown"' in source
    assert 'fileInput.addEventListener("change"' in source
    assert 'Accept: "application/x-ndjson"' in source
