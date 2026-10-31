"""Client für externe MCP-Server (Streamable HTTP), über den Obi fremde Werkzeuge nutzt.

Gegenstück zum eigenen MCP-Server (:mod:`app.services.mcp_http`) und wie dieser nur
mit der stdlib: JSON-RPC 2.0 per ``POST`` an einen einzigen Endpunkt, die Antwort
kommt als ``application/json`` oder als Server-Sent-Events (``text/event-stream``).
Eine vom Server vergebene ``Mcp-Session-Id`` wird mitgeführt; meldet der Server sie
als abgelaufen (HTTP 404), wird die Sitzung einmal neu aufgebaut. Ein HTTP 401 wird
als :class:`McpAuthError` samt ``WWW-Authenticate``-Header gemeldet – daraus findet
:mod:`app.services.mcp_oauth` den Autorisierungsserver.

Schutz bei fremden Servern (gilt auch für die OAuth-Aufrufe):

* Weiterleitungen werden nicht verfolgt – urllib schickte Anmelde-Header sonst an das
  neue Ziel mit, auch über Hostgrenzen und von HTTPS auf HTTP.
* Link-Local-Adressen (Cloud-Metadaten wie 169.254.169.254), Multicast-, reservierte und
  unspezifizierte Adressen sind gesperrt (:func:`check_outbound_url`).
* Je Aufruf gilt eine Frist (``timeout``) für die gesamte Antwort, nicht nur je
  Lesevorgang, dazu eine Größengrenze; ein Server, der tröpfchenweise sendet, hält den
  Chat also nicht unbegrenzt fest.

Nicht unterstützt: der alte HTTP+SSE-Transport (2024-11-05), Anfragen des Servers an
den Client (Sampling, Elicitation, Roots) sowie Ressourcen und Prompts – Obi nutzt nur
Werkzeuge (``tools/list``, ``tools/call``).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import time
from dataclasses import dataclass, field
from http.client import HTTPException
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from app.version import get_version

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "openbuchhaltung-obi"
CLIENT_TITLE = "OpenBuchhaltung (Obi)"
DEFAULT_TIMEOUT_SECONDS = 60.0
# Obergrenzen je Antwort: Ein fremder Server soll den Speicher nicht fluten können.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_LINE_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024
# Höchstens so viele Seiten von tools/list (nextCursor) abrufen.
MAX_TOOL_PAGES = 20
MAX_TOOL_NAME_LENGTH = 128
MAX_TOOL_TITLE_LENGTH = 200
_DEFAULT_PORTS = {"http": 80, "https": 443}


class McpClientError(Exception):
    """Der MCP-Server ist nicht erreichbar oder antwortet nicht protokollgemäß."""


class McpAuthError(McpClientError):
    """Der MCP-Server verlangt eine (neue) Anmeldung (HTTP 401)."""

    def __init__(self, message: str, *, www_authenticate: str | None = None):
        super().__init__(message)
        self.www_authenticate = www_authenticate


class McpRpcError(McpClientError):
    """Der MCP-Server meldet einen JSON-RPC-Fehler (z. B. unbekanntes Werkzeug)."""

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


class _SessionExpired(McpClientError):
    """Der Server kennt die ``Mcp-Session-Id`` nicht mehr (HTTP 404)."""


# ---------------------------------------------------------------------------
# Ausgehende Verbindungen
# ---------------------------------------------------------------------------


class _NoRedirects(HTTPRedirectHandler):
    """Weiterleitungen nicht folgen; urllib meldet den 3xx-Status dann als HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


_OPENER = build_opener(_NoRedirects)


def origin(url: str) -> str:
    """Schema, Host und Port einer URL – für Logs und Anzeigen ohne Pfad/Query, in denen
    gehostete MCP-Server oft ihren Schlüssel tragen."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}"


def resolve_host(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """IP-Adressen eines Hosts (IP-Literale ohne DNS-Abfrage)."""
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]) for info in infos]


def _plain(ip):
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _addresses(url: str):
    parts = urlsplit(url)
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        raise McpClientError("Die URL muss mit http:// oder https:// beginnen.")
    try:
        port = parts.port or _DEFAULT_PORTS[parts.scheme]
    except ValueError as exc:
        raise McpClientError("Die URL enthält einen ungültigen Port.") from exc
    return [_plain(ip) for ip in resolve_host(parts.hostname, port)]


def check_outbound_url(url: str) -> None:
    """Sperrt Ziele, die ein fremder Server nie sein darf: Link-Local (Cloud-Metadaten),
    Multicast, reservierte und unspezifizierte Adressen. Nicht auflösbare Hosts lässt die
    Prüfung durch – der Abruf scheitert dann ohnehin."""
    try:
        addresses = _addresses(url)
    except OSError:
        return
    if any(
        ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved
        for ip in addresses
    ):
        raise McpClientError("Die Adresse des Servers ist nicht zulässig.")


def is_internal_url(url: str) -> bool | None:
    """Ob der Host auf eine nicht öffentliche Adresse zeigt (privat, Loopback, CGNAT,
    Link-Local); ``None``, wenn er nicht auflösbar ist."""
    try:
        addresses = _addresses(url)
    except OSError:
        return None
    return any(not ip.is_global for ip in addresses)


def open_url(request: Request, *, timeout: float):
    """Öffnet eine Anfrage nach Prüfung der Zieladresse, ohne Weiterleitungen."""
    check_outbound_url(request.full_url)
    return _OPENER.open(request, timeout=timeout)


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise McpClientError("Der MCP-Server antwortete nicht rechtzeitig.")


def _chunks(stream, deadline: float):
    """Liest in Blöcken, wie sie ankommen (``read1``), und prüft dazwischen Frist und Größe."""
    read = getattr(stream, "read1", None) or stream.read
    received = 0
    while True:
        _check_deadline(deadline)
        chunk = read(_READ_CHUNK_BYTES)
        if not chunk:
            return
        received += len(chunk)
        if received > MAX_RESPONSE_BYTES:
            raise McpClientError("Die Antwort des MCP-Servers ist zu groß.")
        yield chunk


def read_body(stream, deadline: float) -> bytes:
    return b"".join(_chunks(stream, deadline))


def _lines(stream, deadline: float):
    buffer = b""
    for chunk in _chunks(stream, deadline):
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            yield line
        if len(buffer) > MAX_LINE_BYTES:
            raise McpClientError("Die Antwort des MCP-Servers ist zu groß.")
    if buffer:
        yield buffer


# ---------------------------------------------------------------------------
# Werkzeuge und Ergebnisse
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class McpTool:
    """Werkzeug eines MCP-Servers (aus ``tools/list``)."""

    name: str
    description: str = ""
    title: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def read_only(self) -> bool:
        """Ob der Server das Werkzeug als rein lesend kennzeichnet (``readOnlyHint``)."""
        return self.annotations.get("readOnlyHint") is True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": self.annotations,
        }

    @classmethod
    def from_dict(cls, raw: object) -> McpTool | None:
        if not isinstance(raw, dict):
            return None
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_TOOL_NAME_LENGTH:
            return None
        annotations = raw.get("annotations")
        annotations = annotations if isinstance(annotations, dict) else {}
        title = raw.get("title") or annotations.get("title")
        title = " ".join(title.split())[:MAX_TOOL_TITLE_LENGTH] if isinstance(title, str) else ""
        schema = raw.get("inputSchema")
        description = raw.get("description")
        return cls(
            name=name.strip(),
            description=description if isinstance(description, str) else "",
            title=title or None,
            input_schema=schema if isinstance(schema, dict) else {},
            annotations=annotations,
        )


@dataclass(slots=True)
class McpToolResult:
    """Ergebnis von ``tools/call``: Inhaltsblöcke, strukturierte Daten, Fehlerkennzeichen."""

    content: list[dict[str, Any]] = field(default_factory=list)
    structured_content: Any = None
    is_error: bool = False

    def as_text(self) -> str:
        """Text für das Sprachmodell; Binärinhalte werden nur benannt."""
        parts: list[str] = []
        for block in self.content:
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif kind in {"image", "audio"}:
                label = "Bild" if kind == "image" else "Audio"
                parts.append(f"[{label} ({block.get('mimeType') or 'unbekannt'}) nicht übernommen]")
            elif kind == "resource":
                resource = block.get("resource")
                resource = resource if isinstance(resource, dict) else {}
                uri = resource.get("uri") or ""
                if isinstance(resource.get("text"), str):
                    parts.append(f"[Ressource {uri}]\n{resource['text']}")
                else:
                    mime = resource.get("mimeType") or "unbekannt"
                    parts.append(f"[Ressource {uri} ({mime}), Binärinhalt nicht übernommen]")
            elif kind == "resource_link":
                name = block.get("title") or block.get("name")
                label = f"Verweis {name}" if name else "Verweis"
                parts.append(f"[{label}: {block.get('uri') or ''}]")
        if not parts and self.structured_content is not None:
            parts.append(json.dumps(self.structured_content, ensure_ascii=False))
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# JSON-RPC-Antworten
# ---------------------------------------------------------------------------


def _matches(message: object, request_id: int) -> bool:
    return (
        isinstance(message, dict)
        and ("result" in message or "error" in message)
        and str(message.get("id")) == str(request_id)
    )


def _pick_response(parsed: object, request_id: int) -> dict[str, Any]:
    """Die Antwort zu ``request_id`` aus einer Einzel- oder Batch-Antwort."""
    candidates = parsed if isinstance(parsed, list) else [parsed]
    for message in candidates:
        if _matches(message, request_id):
            return message
    raise McpClientError("Der MCP-Server lieferte keine passende Antwort.")


def _read_event_stream(response, request_id: int, deadline: float) -> dict[str, Any]:
    """Liest Server-Sent-Events bis zur Antwort auf ``request_id``.

    Benachrichtigungen (Fortschritt, Logs) und Anfragen des Servers dazwischen werden
    übergangen – Obi meldet keine Client-Fähigkeiten an.
    """
    data_lines: list[str] = []
    for raw_line in _lines(response, deadline):
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r")
        if line.startswith("data:"):
            data_lines.append(line[5:].removeprefix(" "))
            continue
        if line or not data_lines:
            continue
        data = "\n".join(data_lines)
        data_lines = []
        try:
            message = json.loads(data)
        except ValueError:
            continue
        candidates = message if isinstance(message, list) else [message]
        for candidate in candidates:
            if _matches(candidate, request_id):
                return candidate
    if data_lines:
        try:
            return _pick_response(json.loads("\n".join(data_lines)), request_id)
        except ValueError:
            pass
    raise McpClientError("Der MCP-Server hat die Antwort nicht abgeschlossen.")


# ---------------------------------------------------------------------------
# Sitzung
# ---------------------------------------------------------------------------


class McpSession:
    """Eine Sitzung mit einem MCP-Server (Streamable HTTP).

    ``headers`` trägt die Anmeldung (z. B. ``Authorization: Bearer …``); ``opener``
    ersetzt in Tests :func:`open_url`. :meth:`call_tool` und :meth:`list_tools`
    initialisieren die Sitzung bei Bedarf selbst. ``timeout`` ist die Frist je Aufruf.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self._open = opener or open_url
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] = {}
        self.instructions: str | None = None
        self._initialized = False
        self._next_id = 1

    # -- Protokoll ---------------------------------------------------------

    def initialize(self) -> dict[str, Any]:
        self.session_id = None
        self.protocol_version = None
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": CLIENT_NAME,
                    "title": CLIENT_TITLE,
                    "version": get_version(),
                },
            },
            retry_expired=False,
        )
        version = result.get("protocolVersion")
        self.protocol_version = version if isinstance(version, str) and version else None
        info = result.get("serverInfo")
        self.server_info = info if isinstance(info, dict) else {}
        instructions = result.get("instructions")
        self.instructions = instructions if isinstance(instructions, str) else None
        self._initialized = True
        self._notify("notifications/initialized")
        return result

    def list_tools(self) -> list[McpTool]:
        self._ensure_initialized()
        tools: list[McpTool] = []
        cursor: str | None = None
        for _page in range(MAX_TOOL_PAGES):
            result = self._rpc("tools/list", {"cursor": cursor} if cursor else {})
            raw_tools = result.get("tools")
            for raw in raw_tools if isinstance(raw_tools, list) else ():
                tool = McpTool.from_dict(raw)
                if tool is not None:
                    tools.append(tool)
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                break
            cursor = next_cursor
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> McpToolResult:
        self._ensure_initialized()
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        content = result.get("content")
        return McpToolResult(
            content=[block for block in content if isinstance(block, dict)]
            if isinstance(content, list)
            else [],
            structured_content=result.get("structuredContent"),
            is_error=bool(result.get("isError")),
        )

    def reset(self, *, headers: dict[str, str] | None = None) -> None:
        """Sitzung verwerfen (z. B. nach einer neuen Anmeldung) und neu aufbauen lassen."""
        self.close()
        if headers is not None:
            self.headers = dict(headers)

    def close(self) -> None:
        """Beendet die Sitzung beim Server (``DELETE``, falls er eine Sitzungs-ID vergab)."""
        session_id, self.session_id = self.session_id, None
        self._initialized = False
        if not session_id:
            return
        headers = {**self.headers, "Mcp-Session-Id": session_id}
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        try:
            with self._open(
                Request(self.url, headers=headers, method="DELETE"), timeout=5
            ) as response:
                response.read(1024)
        except (McpClientError, HTTPError, URLError, OSError, HTTPException, ValueError):
            pass  # Server ohne explizites Sitzungsende (405) oder schon getrennt

    # -- Transport ---------------------------------------------------------

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

    def _rpc(
        self, method: str, params: dict[str, Any], *, retry_expired: bool = True
    ) -> dict[str, Any]:
        try:
            response = self._send_request(method, params)
        except _SessionExpired:
            if not retry_expired:
                raise
            self.initialize()
            response = self._send_request(method, params)
        error = response.get("error")
        if error is not None:
            error = error if isinstance(error, dict) else {}
            message = str(error.get("message") or "Unbekannter Fehler")[:500]
            code = error.get("code") if isinstance(error.get("code"), int) else None
            raise McpRpcError(f"Der MCP-Server meldet: {message}", code=code)
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    def _send_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        return self._post(message, request_id=request_id)

    def _notify(self, method: str) -> None:
        self._post({"jsonrpc": "2.0", "method": method}, request_id=None)

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _post(self, message: dict[str, Any], *, request_id: int | None) -> dict[str, Any]:
        request = Request(
            self.url,
            data=json.dumps(message, ensure_ascii=False).encode("utf-8"),
            headers=self._request_headers(),
            method="POST",
        )
        deadline = time.monotonic() + self.timeout
        try:
            with self._open(request, timeout=self.timeout) as response:
                if message.get("method") == "initialize":
                    session_id = response.headers.get("Mcp-Session-Id")
                    self.session_id = session_id.strip()[:256] if session_id else None
                if request_id is None:
                    return {}
                content_type = response.headers.get("Content-Type") or ""
                if "text/event-stream" in content_type:
                    return _read_event_stream(response, request_id, deadline)
                body = read_body(response, deadline)
        except HTTPError as exc:
            raise self._http_error(exc, message) from exc
        except TimeoutError as exc:
            raise McpClientError("Der MCP-Server antwortete nicht rechtzeitig.") from exc
        except URLError as exc:
            raise McpClientError("Der MCP-Server ist nicht erreichbar.") from exc
        except (OSError, HTTPException) as exc:
            raise McpClientError("Die Verbindung zum MCP-Server wurde unterbrochen.") from exc
        if not body.strip():
            raise McpClientError("Der MCP-Server lieferte keine Antwort.")
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise McpClientError("Der MCP-Server lieferte kein gültiges JSON.") from exc
        return _pick_response(parsed, request_id)

    def _http_error(self, exc: HTTPError, message: dict[str, Any]) -> Exception:
        if exc.code == 401:
            www_authenticate = exc.headers.get("WWW-Authenticate") if exc.headers else None
            return McpAuthError(
                "Der MCP-Server verlangt eine Anmeldung.", www_authenticate=www_authenticate
            )
        if exc.code == 403:
            # Auch ein Scope-Fehler (insufficient_scope) ist kein Grund, die Anmeldung zu
            # verwerfen: Die übrigen Werkzeuge funktionieren damit weiter.
            return McpClientError(
                "Der MCP-Server verweigert den Zugriff – dem Zugang fehlen Rechte dafür."
            )
        if exc.code == 404 and self.session_id and message.get("method") != "initialize":
            return _SessionExpired("Der MCP-Server hat die Sitzung abgelehnt.")
        if 300 <= exc.code < 400:
            return McpClientError(
                "Der MCP-Server leitet auf eine andere Adresse weiter – bitte die "
                "endgültige URL eintragen."
            )
        try:
            detail = exc.read(2000).decode("utf-8", errors="replace")
        except (OSError, HTTPException, ValueError):
            detail = ""
        # Fehlertexte fremder Server nur ins Log, nicht an das Modell.
        logger.warning(
            "MCP-Server %s antwortete mit HTTP %s: %s", origin(self.url), exc.code, detail[:300]
        )
        return McpClientError(f"Der MCP-Server antwortete mit HTTP {exc.code}.")
