"""Wissensbasis für Obi: Fachwissen als Markdown-Abschnitte mit Volltextsuche.

Die Dateien liegen unter ``data/wissen`` (``*.md``). Jede Datei beginnt mit
``# Titel`` und optionalen Kopfzeilen (``Rechtsstand: 2026-10``, ``Quellen: …``,
``Hinweis: …``); jeder ``## Abschnitt`` ist eine Sucheinheit und kann als erste
Zeile ``Schlagworte: …`` tragen, die nur den Index füttern.

Die ``kompakt*.md``-Dateien gehen nicht in den Suchindex, sondern komplett in
den Systemprompt des KI-Chats (``kompakt.md`` immer, ``kompakt-skr03.md`` bzw.
``kompakt-skr04.md`` je Kontenrahmen der Gesellschaft).

Gesucht wird lexikalisch (BM25 über Abschnitte, mit Umlaut-Faltung, leichter
Suffix-Kürzung und Präfix-Treffern für zusammengesetzte Wörter). Eine
Vektordatenbank braucht die kuratierte Basis von wenigen hundert Abschnitten
nicht: Das Modell formuliert Fachbegriffe, die in den Schlagworten stehen, und
der Index entsteht beim Start aus den Dateien.

``KNOWLEDGE_EXTRA_DIR`` nennt ein weiteres Verzeichnis mit gleich aufgebauten
Dateien (instanzeigene Notizen); dessen ``kompakt*.md`` werden angehängt.
"""

from __future__ import annotations

import math
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

KNOWLEDGE_DIR = Path(__file__).resolve().parents[2] / "data" / "wissen"
EXTRA_DIR_ENV = "KNOWLEDGE_EXTRA_DIR"

COMPACT_GENERAL = "kompakt"
COMPACT_CHART_PREFIX = "kompakt-"

DEFAULT_SEARCH_LIMIT = 3
MAX_SEARCH_LIMIT = 8

_META_KEYS = ("rechtsstand", "quellen", "hinweis")
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "und",
        "oder",
        "der",
        "die",
        "das",
        "des",
        "dem",
        "den",
        "ein",
        "eine",
        "einer",
        "eines",
        "einem",
        "einen",
        "ist",
        "sind",
        "wird",
        "werden",
        "bei",
        "mit",
        "von",
        "vom",
        "zum",
        "zur",
        "auf",
        "aus",
        "fuer",
        "fur",
        "nach",
        "ueber",
        "uber",
        "als",
        "auch",
        "nicht",
        "nur",
        "wie",
        "was",
        "wenn",
        "dann",
        "sich",
        "sie",
        "ich",
        "wir",
        "ihr",
        "es",
        "im",
        "in",
        "am",
        "an",
        "zu",
        "um",
        "so",
        "ob",
        "noch",
        "bis",
        "je",
        "pro",
        "the",
        "of",
        "to",
        "for",
        "and",
        "kann",
        "muss",
        "darf",
        "soll",
        "habe",
        "haben",
        "hat",
        "wurde",
        "wurden",
        "welche",
        "welcher",
        "welches",
        "wer",
        "wo",
        "wann",
        "warum",
        "wieviel",
        "wie viel",
        "bitte",
        "mir",
        "mich",
        "uns",
        "ihnen",
        "ihm",
        "diese",
        "dieser",
        "dieses",
        "jede",
        "jeder",
        "jedes",
    }
)
_SUFFIXES = (
    "ungen",
    "ung",
    "heiten",
    "heit",
    "keiten",
    "keit",
    "innen",
    "en",
    "er",
    "es",
    "em",
    "e",
    "n",
    "s",
)


def _fold(text: str) -> str:
    text = text.casefold().translate(_UMLAUTS)
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _stem(token: str) -> str:
    if token.isdigit() or len(token) <= 4:
        return token
    for suffix in _SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def tokenize(text: str) -> list[str]:
    """Suchbegriffe eines Textes: klein, Umlaute gefaltet, ohne Stoppwörter, gekürzt."""
    tokens = []
    for raw in _TOKEN_RE.findall(_fold(text)):
        if raw in _STOPWORDS or (len(raw) < 2 and not raw.isdigit()):
            continue
        tokens.append(_stem(raw))
    return tokens


def _slug(text: str) -> str:
    folded = re.sub(r"[^a-z0-9]+", "-", _fold(text)).strip("-")
    return folded or "abschnitt"


@dataclass(frozen=True, slots=True)
class KnowledgeSection:
    """Ein ``##``-Abschnitt einer Wissensdatei."""

    id: str
    file: str
    source_title: str
    title: str
    rechtsstand: str | None
    keywords: tuple[str, ...]
    text: str

    def to_dict(self, *, with_text: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": self.id,
            "file": self.file,
            "source_title": self.source_title,
            "title": self.title,
            "rechtsstand": self.rechtsstand,
        }
        if with_text:
            data["text"] = self.text
        return data


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    file: str
    title: str
    rechtsstand: str | None
    meta: dict[str, str]
    sections: tuple[KnowledgeSection, ...]
    body: str  # Volltext ohne Titel- und Kopfzeilen (für kompakt*.md)


def parse_document(file_stem: str, text: str) -> KnowledgeDocument:
    """Zerlegt eine Markdown-Wissensdatei in Titel, Kopfzeilen und Abschnitte."""
    lines = text.splitlines()
    title = file_stem
    meta: dict[str, str] = {}
    sections: list[KnowledgeSection] = []
    body_lines: list[str] = []
    current_title: str | None = None
    current_lines: list[str] = []
    seen_slugs: Counter[str] = Counter()

    def flush() -> None:
        if current_title is None:
            return
        keywords: list[str] = []
        content: list[str] = []
        for line in current_lines:
            if line.lower().startswith("schlagworte:"):
                keywords.extend(
                    part.strip() for part in line.split(":", 1)[1].split(",") if part.strip()
                )
            else:
                content.append(line)
        slug = _slug(current_title)
        seen_slugs[slug] += 1
        if seen_slugs[slug] > 1:
            slug = f"{slug}-{seen_slugs[slug]}"
        sections.append(
            KnowledgeSection(
                id=f"{file_stem}/{slug}",
                file=file_stem,
                source_title=title,
                title=current_title,
                rechtsstand=meta.get("rechtsstand"),
                keywords=tuple(keywords),
                text="\n".join(content).strip(),
            )
        )

    for line in lines:
        if line.startswith("# ") and current_title is None and not sections and title == file_stem:
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            flush()
            current_title = line[3:].strip()
            current_lines = []
            continue
        if current_title is None:
            body_lines.append(line)
            lowered = line.lower()
            for key in _META_KEYS:
                if lowered.startswith(f"{key}:"):
                    meta[key] = line.split(":", 1)[1].strip()
            continue
        current_lines.append(line)
    flush()
    # Volltext ohne Titel- und Kopfzeilen (für die kompakt*.md im Systemprompt).
    body_parts: list[str] = []
    for line in lines:
        if line.startswith("# ") and not body_parts and line[2:].strip() == title:
            continue
        lowered = line.lower()
        if not body_parts and any(lowered.startswith(f"{key}:") for key in _META_KEYS):
            continue
        body_parts.append(line)
    return KnowledgeDocument(
        file=file_stem,
        title=title,
        rechtsstand=meta.get("rechtsstand"),
        meta=meta,
        sections=tuple(sections),
        body="\n".join(body_parts).strip(),
    )


def _is_compact(file_stem: str) -> bool:
    return file_stem == COMPACT_GENERAL or file_stem.startswith(COMPACT_CHART_PREFIX)


class KnowledgeBase:
    """Abschnittsindex (BM25) plus Kompakttexte für den Systemprompt."""

    K1 = 1.5
    B = 0.75
    KEYWORD_WEIGHT = 3
    TITLE_WEIGHT = 2
    PREFIX_WEIGHT = 0.5
    MIN_PREFIX_LEN = 5

    def __init__(self, documents: Iterable[KnowledgeDocument]) -> None:
        self.documents: list[KnowledgeDocument] = []
        self.compact: dict[str, list[KnowledgeDocument]] = {}
        self.sections: list[KnowledgeSection] = []
        for document in documents:
            if _is_compact(document.file):
                chart = (
                    document.file[len(COMPACT_CHART_PREFIX) :]
                    if document.file != COMPACT_GENERAL
                    else ""
                )
                self.compact.setdefault(chart, []).append(document)
                continue
            self.documents.append(document)
            self.sections.extend(document.sections)
        self._by_id = {section.id: section for section in self.sections}
        self._term_freqs: list[Counter[str]] = []
        self._doc_lengths: list[int] = []
        self._doc_freq: Counter[str] = Counter()
        for section in self.sections:
            tokens = tokenize(section.text)
            tokens += tokenize(section.title) * self.TITLE_WEIGHT
            tokens += tokenize(" ".join(section.keywords)) * self.KEYWORD_WEIGHT
            counts = Counter(tokens)
            self._term_freqs.append(counts)
            self._doc_lengths.append(sum(counts.values()))
            self._doc_freq.update(counts.keys())
        self._avg_length = (
            sum(self._doc_lengths) / len(self._doc_lengths) if self._doc_lengths else 1.0
        )
        self._vocabulary = sorted(self._doc_freq)

    # -- Zugriff -----------------------------------------------------------

    def get(self, section_id: str) -> KnowledgeSection | None:
        return self._by_id.get(section_id)

    def rechtsstand(self) -> str | None:
        """Jüngster Rechtsstand aller Dateien (JJJJ-MM sortiert als Text)."""
        values = [doc.rechtsstand for doc in self.documents if doc.rechtsstand]
        values += [
            doc.rechtsstand for docs in self.compact.values() for doc in docs if doc.rechtsstand
        ]
        return max(values) if values else None

    def toc(self) -> list[dict[str, Any]]:
        return [
            {
                "file": document.file,
                "title": document.title,
                "rechtsstand": document.rechtsstand,
                "sections": [
                    {"id": section.id, "title": section.title} for section in document.sections
                ],
            }
            for document in self.documents
        ]

    def compact_text(self, chart: str | None) -> str:
        """Kompaktwissen für den Systemprompt; ohne bekannten Kontenrahmen beide Kurzlisten."""
        parts = [doc.body for doc in self.compact.get("", [])]
        charts = (
            [chart]
            if chart and chart in self.compact
            else sorted(key for key in self.compact if key)
        )
        for key in charts:
            parts.extend(doc.body for doc in self.compact.get(key, []))
        return "\n\n".join(part for part in parts if part)

    # -- Suche -------------------------------------------------------------

    def _idf(self, term: str) -> float:
        n = len(self.sections)
        df = self._doc_freq.get(term, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def _expand(self, token: str) -> dict[str, float]:
        """Exakter Treffer voll, Präfix-Treffer (zusammengesetzte Wörter) halb."""
        weights: dict[str, float] = {}
        if token in self._doc_freq:
            weights[token] = 1.0
        if len(token) >= self.MIN_PREFIX_LEN:
            for term in self._vocabulary:
                if term == token:
                    continue
                if (term.startswith(token) or token.startswith(term)) and len(
                    term
                ) >= self.MIN_PREFIX_LEN:
                    weights[term] = max(weights.get(term, 0.0), self.PREFIX_WEIGHT)
        return weights

    def search(
        self, query: str, *, limit: int = DEFAULT_SEARCH_LIMIT
    ) -> list[tuple[KnowledgeSection, float]]:
        limit = max(1, min(int(limit or DEFAULT_SEARCH_LIMIT), MAX_SEARCH_LIMIT))
        query_tokens = tokenize(query)
        if not query_tokens or not self.sections:
            return []
        expanded: dict[str, float] = {}
        for token in set(query_tokens):
            for term, weight in self._expand(token).items():
                expanded[term] = max(expanded.get(term, 0.0), weight)
        if not expanded:
            return []
        scores: list[tuple[float, int]] = []
        for index, counts in enumerate(self._term_freqs):
            score = 0.0
            length_norm = self.K1 * (
                1 - self.B + self.B * self._doc_lengths[index] / self._avg_length
            )
            for term, weight in expanded.items():
                tf = counts.get(term)
                if not tf:
                    continue
                score += weight * self._idf(term) * (tf * (self.K1 + 1)) / (tf + length_norm)
            if score > 0:
                scores.append((score, index))
        scores.sort(key=lambda item: (-item[0], item[1]))
        return [(self.sections[index], round(score, 3)) for score, index in scores[:limit]]


def _markdown_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.glob("*.md") if path.is_file())


def load_knowledge_base(directories: Iterable[Path] | None = None) -> KnowledgeBase:
    """Lädt alle Wissensdateien; Standard: ``data/wissen`` plus ``KNOWLEDGE_EXTRA_DIR``."""
    if directories is None:
        directories = [KNOWLEDGE_DIR]
        extra = os.environ.get(EXTRA_DIR_ENV, "").strip()
        if extra:
            directories.append(Path(extra))
    documents: list[KnowledgeDocument] = []
    seen: set[str] = set()
    for directory in directories:
        for path in _markdown_files(Path(directory)):
            stem = path.stem
            if stem in seen and not _is_compact(stem):
                stem = f"{stem}-{len(seen)}"
            seen.add(stem)
            documents.append(parse_document(stem, path.read_text(encoding="utf-8")))
    return KnowledgeBase(documents)


@lru_cache(maxsize=1)
def get_knowledge_base() -> KnowledgeBase:
    return load_knowledge_base()


def reset_knowledge_base_cache() -> None:
    get_knowledge_base.cache_clear()


def search_knowledge(query: str | None, *, limit: int = DEFAULT_SEARCH_LIMIT) -> dict[str, Any]:
    """Ergebnis für API/MCP/Chat: Treffer mit Text, ohne Suchbegriff das Inhaltsverzeichnis."""
    base = get_knowledge_base()
    query = (query or "").strip()
    if not query:
        return {
            "query": "",
            "rechtsstand": base.rechtsstand(),
            "results": [],
            "toc": base.toc(),
        }
    hits = base.search(query, limit=limit)
    return {
        "query": query,
        "rechtsstand": base.rechtsstand(),
        "results": [{**section.to_dict(), "score": score} for section, score in hits],
        "toc": [] if hits else base.toc(),
    }
