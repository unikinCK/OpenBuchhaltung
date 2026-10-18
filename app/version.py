"""Versions- und Build-Informationen der Anwendung.

Einzige Quelle der Versionsnummer ist ``[project] version`` in ``pyproject.toml``
(SemVer; Releases tragen den Git-Tag ``v<version>``). Der Commit stammt aus der
Umgebungsvariable ``GIT_COMMIT`` (im Docker-Image als Build-Arg hinterlegt) oder,
für lokale Checkouts, aus ``git rev-parse HEAD``.
"""

from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FALLBACK_VERSION = "0.0.0+unknown"
_VERSION_PATTERN = re.compile(
    r"^\[project\].*?^version\s*=\s*\"([^\"]+)\"", re.MULTILINE | re.DOTALL
)


@lru_cache(maxsize=1)
def get_version() -> str:
    """Liest die Version aus ``pyproject.toml`` (``0.0.0+unknown`` als Fallback)."""
    try:
        text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return FALLBACK_VERSION

    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - nur Interpreter < 3.11
        tomllib = None  # type: ignore[assignment]

    if tomllib is not None:
        try:
            version = tomllib.loads(text).get("project", {}).get("version")
        except tomllib.TOMLDecodeError:
            version = None
        if version:
            return str(version)

    match = _VERSION_PATTERN.search(text)
    return match.group(1) if match else FALLBACK_VERSION


@lru_cache(maxsize=1)
def get_commit() -> str | None:
    """Commit-SHA des laufenden Standes: ``GIT_COMMIT`` oder lokaler Git-Checkout."""
    from_env = (os.environ.get("GIT_COMMIT") or "").strip()
    if from_env:
        return from_env
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    commit = completed.stdout.strip()
    return commit or None
