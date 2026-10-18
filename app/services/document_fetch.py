"""Belege per Link laden (``source_url`` statt ``content_base64``).

Ein Sprachmodell, das einen Beleg aus einem anderen System übergibt (z. B. den
``downloadUrl`` eines Mail-Anhangs aus mcp-imap), müsste die Datei sonst als
Base64 durch seinen Kontext schleusen: Ein 200-KB-PDF wären über 100k Tokens.
Mit ``source_url`` lädt der Server die Datei selbst.

Schutz gegen SSRF:

* nur ``http``/``https``, Host:Port muss in ``DOCUMENT_FETCH_ALLOWED_HOSTS`` stehen
  (ohne Konfiguration ist ``source_url`` abgeschaltet);
* der Hostname wird einmal aufgelöst und die Verbindung geht genau an diese Adresse
  (kein DNS-Rebinding zwischen Prüfung und Abruf);
* private, Loopback- und CGNAT-Adressen (z. B. Tailscale) nur bei exakt eingetragenem
  Host; Platzhalter-Einträge (``*.example.com``) erlauben nur öffentliche Adressen,
  Link-Local (Cloud-Metadaten), Multicast und reservierte Bereiche nie;
* Weiterleitungen nur auf denselben Host und Port (höchstens drei);
* Zeitlimit für den gesamten Abruf, Größenlimit beim Streamen.
"""

from __future__ import annotations

import http.client
import ipaddress
import mimetypes
import socket
import ssl
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from email.message import Message
from email.utils import collapse_rfc2231_value
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit

DEFAULT_FETCH_TIMEOUT_SECONDS = 30.0
MAX_REDIRECTS = 3
_CHUNK_BYTES = 64 * 1024
_DEFAULT_PORTS = {"http": 80, "https": 443}
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_FALLBACK_HINT = "Datei stattdessen als content_base64 senden"

# Typen, die einen Dateinamen-Rückschluss auf den MIME-Type erlauben.
_EXTENSION_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}


class DocumentFetchError(Exception):
    """Abruf über ``source_url`` gescheitert; ``code`` ist maschinenlesbar."""

    def __init__(self, message: str, *, code: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class AllowedHost:
    host: str  # kleingeschrieben; bei Platzhaltern ohne "*."
    port: int | None  # None = Standardport des Schemas
    wildcard: bool = False

    def matches(self, host: str, port: int, scheme: str) -> bool:
        expected_port = self.port if self.port is not None else _DEFAULT_PORTS[scheme]
        if port != expected_port:
            return False
        if self.wildcard:
            return host.endswith("." + self.host)
        return host == self.host


@dataclass(frozen=True, slots=True)
class FetchedDocument:
    content: bytes
    file_name: str | None
    mime_type: str | None
    url: str


Resolver = Callable[[str, int], list[str]]


def parse_allowed_hosts(raw: str | Iterable[str] | None) -> tuple[AllowedHost, ...]:
    """``host:port`` (kommagetrennt); ``*.domain`` erlaubt Subdomains."""
    if raw is None:
        return ()
    entries = raw.split(",") if isinstance(raw, str) else list(raw)
    allowed: list[AllowedHost] = []
    for entry in entries:
        value = entry.strip().lower()
        if not value:
            continue
        # urlsplit zerlegt auch [IPv6]:port korrekt.
        parts = urlsplit(f"//{value}")
        try:
            port = parts.port
        except ValueError as exc:
            raise ValueError(f"Ungültiger Port in DOCUMENT_FETCH_ALLOWED_HOSTS: {entry!r}") from exc
        host = parts.hostname or ""
        wildcard = host.startswith("*.")
        if wildcard:
            host = host[2:]
        if not host or "*" in host or "/" in value:
            raise ValueError(f"Ungültiger Eintrag in DOCUMENT_FETCH_ALLOWED_HOSTS: {entry!r}")
        allowed.append(AllowedHost(host=host, port=port, wildcard=wildcard))
    return tuple(allowed)


def _default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _check_address(address: str, *, explicit: bool) -> None:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        raise DocumentFetchError(
            f"source_url zeigt auf eine gesperrte Adresse ({ip}). {_FALLBACK_HINT}.",
            code="address_not_allowed",
            status_code=400,
        )
    if not ip.is_global and not explicit:
        raise DocumentFetchError(
            f"source_url zeigt auf eine interne Adresse ({ip}); interne Adressen sind nur "
            "für ausdrücklich eingetragene Hosts erlaubt (nicht über *.-Einträge). "
            f"{_FALLBACK_HINT}.",
            code="address_not_allowed",
            status_code=400,
        )


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, *, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._pinned_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self, host: str, port: int, *, address: str, timeout: float, context: ssl.SSLContext
    ) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self._pinned_address = address
        self._tls_context = context

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_address, self.port), self.timeout)
        # Zertifikat und SNI gegen den Hostnamen, nicht gegen die IP prüfen.
        self.sock = self._tls_context.wrap_socket(sock, server_hostname=self.host)


def _file_name_from_response(response: http.client.HTTPResponse, url: str) -> str | None:
    disposition = response.getheader("Content-Disposition")
    if disposition:
        message = Message()
        message["Content-Disposition"] = disposition
        # filename*=UTF-8''… (RFC 2231, als Tupel dekodiert) hat Vorrang vor filename
        # (RFC 6266), die Standardbibliothek nimmt sonst das erste Vorkommen.
        names = [
            value
            for key, value in (message.get_params(header="content-disposition") or [])[1:]
            if key.lower() == "filename" and value
        ]
        names.sort(key=lambda value: not isinstance(value, tuple))
        if names:
            file_name = collapse_rfc2231_value(names[0])
            return PurePosixPath(file_name.replace("\\", "/")).name or None
    segment = PurePosixPath(unquote(urlsplit(url).path)).name
    return segment or None


def _mime_type_from_response(
    response: http.client.HTTPResponse, file_name: str | None
) -> str | None:
    content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
    if content_type and content_type not in {"application/octet-stream", "binary/octet-stream"}:
        return content_type
    if file_name:
        suffix = PurePosixPath(file_name).suffix.lower()
        return _EXTENSION_MIME_TYPES.get(suffix) or mimetypes.guess_type(file_name)[0]
    return None


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DocumentFetchError(
            f"Zeitlimit beim Laden von source_url überschritten. {_FALLBACK_HINT}.",
            code="timeout",
            status_code=504,
        )
    return remaining


def fetch_document(
    url: str,
    *,
    allowed_hosts: tuple[AllowedHost, ...],
    max_bytes: int,
    timeout: float = DEFAULT_FETCH_TIMEOUT_SECONDS,
    resolver: Resolver | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> FetchedDocument:
    """Lädt ``url`` unter den oben beschriebenen Schutzregeln."""
    if not allowed_hosts:
        raise DocumentFetchError(
            "source_url ist auf diesem Server nicht freigeschaltet "
            f"(DOCUMENT_FETCH_ALLOWED_HOSTS ist leer). {_FALLBACK_HINT}.",
            code="disabled",
            status_code=400,
        )
    resolver = resolver or _default_resolver
    deadline = time.monotonic() + timeout

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise DocumentFetchError(
            f"source_url ist keine gültige URL. {_FALLBACK_HINT}.",
            code="invalid_url",
            status_code=400,
        ) from exc
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host:
        raise DocumentFetchError(
            f"source_url muss eine http(s)-URL sein. {_FALLBACK_HINT}.",
            code="invalid_url",
            status_code=400,
        )
    if parts.username or parts.password:
        raise DocumentFetchError(
            f"source_url darf keine Zugangsdaten enthalten. {_FALLBACK_HINT}.",
            code="invalid_url",
            status_code=400,
        )
    port = port or _DEFAULT_PORTS[scheme]
    entry = next((item for item in allowed_hosts if item.matches(host, port, scheme)), None)
    if entry is None:
        raise DocumentFetchError(
            f"Host {host}:{port} ist für source_url nicht freigegeben "
            f"(DOCUMENT_FETCH_ALLOWED_HOSTS). {_FALLBACK_HINT}.",
            code="host_not_allowed",
            status_code=400,
        )

    try:
        addresses = resolver(host, port)
    except OSError as exc:
        raise DocumentFetchError(
            f"Host {host} von source_url ist nicht auflösbar. {_FALLBACK_HINT}.",
            code="unreachable",
            status_code=502,
        ) from exc
    if not addresses:
        raise DocumentFetchError(
            f"Host {host} von source_url ist nicht auflösbar. {_FALLBACK_HINT}.",
            code="unreachable",
            status_code=502,
        )
    for address in addresses:
        _check_address(address, explicit=not entry.wildcard)
    address = addresses[0]

    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        current = urlsplit(current_url)
        target = current.path or "/"
        if current.query:
            target = f"{target}?{current.query}"
        if scheme == "https":
            connection: http.client.HTTPConnection = _PinnedHTTPSConnection(
                host,
                port,
                address=address,
                timeout=_remaining(deadline),
                context=ssl_context or ssl.create_default_context(),
            )
        else:
            connection = _PinnedHTTPConnection(
                host, port, address=address, timeout=_remaining(deadline)
            )
        try:
            try:
                connection.request(
                    "GET",
                    target,
                    headers={"Accept": "application/pdf, image/jpeg, image/png, */*"},
                )
                response = connection.getresponse()
            except (TimeoutError, socket.timeout) as exc:
                raise DocumentFetchError(
                    f"Zeitlimit beim Laden von source_url überschritten. {_FALLBACK_HINT}.",
                    code="timeout",
                    status_code=504,
                ) from exc
            except (OSError, http.client.HTTPException) as exc:
                raise DocumentFetchError(
                    f"source_url nicht erreichbar ({exc.__class__.__name__}). {_FALLBACK_HINT}.",
                    code="unreachable",
                    status_code=502,
                ) from exc

            if response.status in _REDIRECT_STATUSES:
                location = response.getheader("Location") or ""
                next_url = urljoin(current_url, location)
                next_parts = urlsplit(next_url)
                try:
                    next_port = next_parts.port or _DEFAULT_PORTS.get(next_parts.scheme.lower())
                except ValueError:
                    next_port = None
                if (
                    not location
                    or next_parts.scheme.lower() != scheme
                    or (next_parts.hostname or "").lower() != host
                    or next_port != port
                ):
                    raise DocumentFetchError(
                        "source_url leitet auf einen anderen Host weiter; Weiterleitungen sind "
                        f"nur innerhalb desselben Hosts erlaubt. {_FALLBACK_HINT}.",
                        code="redirect_not_allowed",
                        status_code=502,
                    )
                current_url = next_url
                continue

            if response.status in (404, 410):
                raise DocumentFetchError(
                    f"source_url liefert HTTP {response.status}: Link abgelaufen oder Datei "
                    "nicht vorhanden. Neuen Link holen oder die Datei als content_base64 "
                    "senden.",
                    code="not_found",
                    status_code=502,
                )
            if not 200 <= response.status < 300:
                raise DocumentFetchError(
                    f"source_url liefert HTTP {response.status}. {_FALLBACK_HINT}.",
                    code="http_error",
                    status_code=502,
                )

            too_large = DocumentFetchError(
                f"Datei unter source_url ist größer als {max_bytes} Bytes.",
                code="too_large",
                status_code=413,
            )
            declared_length = response.getheader("Content-Length")
            if declared_length and declared_length.isdigit() and int(declared_length) > max_bytes:
                raise too_large
            chunks: list[bytes] = []
            received = 0
            while True:
                if connection.sock is not None:
                    connection.sock.settimeout(_remaining(deadline))
                try:
                    chunk = response.read(_CHUNK_BYTES)
                except (TimeoutError, socket.timeout) as exc:
                    raise DocumentFetchError(
                        f"Zeitlimit beim Laden von source_url überschritten. {_FALLBACK_HINT}.",
                        code="timeout",
                        status_code=504,
                    ) from exc
                except (OSError, http.client.HTTPException) as exc:
                    raise DocumentFetchError(
                        f"Abruf von source_url abgebrochen ({exc.__class__.__name__}). "
                        f"{_FALLBACK_HINT}.",
                        code="unreachable",
                        status_code=502,
                    ) from exc
                if not chunk:
                    break
                received += len(chunk)
                if received > max_bytes:
                    raise too_large
                chunks.append(chunk)
            _remaining(deadline)

            file_name = _file_name_from_response(response, current_url)
            return FetchedDocument(
                content=b"".join(chunks),
                file_name=file_name,
                mime_type=_mime_type_from_response(response, file_name),
                url=current_url,
            )
        finally:
            connection.close()

    raise DocumentFetchError(
        f"source_url leitet zu oft weiter. {_FALLBACK_HINT}.",
        code="redirect_not_allowed",
        status_code=502,
    )
