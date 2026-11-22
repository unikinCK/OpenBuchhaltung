"""ctypes-Anbindung an die ERiC-Bibliothek (ELSTER Rich Client).

ERiC ist eine C-Bibliothek der Finanzverwaltung; sie liegt nicht im Repo, sondern wird beim
Betrieb eingebunden (ADR-011). Dieses Modul kapselt die wenigen Funktionen, die OpenBuchhaltung
braucht: Initialisieren, Datensatz prüfen bzw. senden (``EricBearbeiteVorgang``), Zertifikat
öffnen und Fehlertexte holen. Es wird nur im ERiC-Runner-Prozess geladen
(``app.services.eric_runner``), nie im Webprozess: ERiC hält globalen Zustand je Prozess.
"""

from __future__ import annotations

import ctypes
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

ERIC_OK = 0
ERIC_GLOBAL_PRUEF_FEHLER = 610001002
ERIC_GLOBAL_HINWEISE = 610001003

ERIC_VALIDIERE = 1 << 1
ERIC_SENDE = 1 << 2
ERIC_DRUCKE = 1 << 5

_PRINT_PARAMETER_VERSION = 4
_CRYPTO_PARAMETER_VERSION = 3


class EricError(RuntimeError):
    """ERiC konnte nicht geladen werden oder ein Aufruf ist gescheitert."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class _PrintParameters(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("vorschau", ctypes.c_uint32),
        ("duplexDruck", ctypes.c_uint32),
        ("pdfName", ctypes.c_char_p),
        ("fussText", ctypes.c_char_p),
        ("pdfCallback", ctypes.c_void_p),
        ("pdfCallbackBenutzerdaten", ctypes.c_void_p),
    ]


class _CryptoParameters(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint32),
        ("zertifikatHandle", ctypes.c_uint32),
        ("pin", ctypes.c_char_p),
    ]


@dataclass(frozen=True, slots=True)
class EricResult:
    """Ergebnis eines ``EricBearbeiteVorgang``-Aufrufs."""

    code: int
    message: str
    response_xml: str
    server_response_xml: str
    transfer_ticket: str | None = None
    findings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.code == ERIC_OK


def library_file_name() -> str:
    if sys.platform == "darwin":
        return "libericapi.dylib"
    if sys.platform.startswith("win"):
        return "ericapi.dll"
    return "libericapi.so"


def resolve_library_dir(path: str | Path) -> Path:
    """``ELSTER_ERIC_LIBRARY_PATH`` darf auf ``lib/`` oder direkt auf ``libericapi`` zeigen."""
    candidate = Path(path).expanduser()
    directory = candidate if candidate.is_dir() else candidate.parent
    if not (directory / library_file_name()).exists():
        raise EricError(f"ERiC-Bibliothek {library_file_name()} nicht gefunden in {directory}.")
    return directory


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_findings(response_xml: str) -> list[str]:
    """Fehler- und Hinweistexte aus der Rückgabe-XML von ``EricBearbeiteVorgang``."""
    if not response_xml.strip():
        return []
    try:
        root = ET.fromstring(response_xml)
    except ET.ParseError:
        return []
    findings: list[str] = []
    for element in root.iter():
        if _local(element.tag) not in {"FehlerRegelpruefung", "Hinweis"}:
            continue
        texts = {_local(child.tag): (child.text or "").strip() for child in element}
        text = _FIELD_REFERENCE_RE.sub(r"\1", texts.get("Text") or "")
        field_path = texts.get("Feldidentifikator") or ""
        field_name = re.sub(r"\[\d+\]$", "", field_path.rsplit("/", 1)[-1])
        if text:
            findings.append(f"{field_name}: {text}" if field_name else text)
    return findings


# ``'$/Elster[1]/…/Steuernummer[1]$'`` im Fehlertext → ``Steuernummer``.
_FIELD_REFERENCE_RE = re.compile(r"\$[^$]*?/([A-Za-z0-9_]+)\[\d+\]\$")


def parse_transfer_ticket(*xml_documents: str) -> str | None:
    for document in xml_documents:
        match = re.search(r"<(?:\w+:)?TransferTicket>([^<]+)</", document or "")
        if match:
            return match.group(1).strip()
    return None


class EricClient:
    """Lädt ERiC einmal je Prozess; als Kontextmanager mit ``EricBeende`` am Ende."""

    def __init__(self, library_path: str | Path, log_dir: str | Path) -> None:
        self.library_dir = resolve_library_dir(library_path)
        self.log_dir = Path(log_dir)
        self._lib: ctypes.CDLL | None = None

    def __enter__(self) -> EricClient:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        try:
            lib = ctypes.CDLL(str(self.library_dir / library_file_name()))
        except OSError as exc:
            raise EricError(f"ERiC-Bibliothek nicht ladbar: {exc}") from exc
        self._declare(lib)
        code = lib.EricInitialisiere(
            str(self.library_dir).encode("utf-8"), str(self.log_dir).encode("utf-8")
        )
        if code != ERIC_OK:
            raise EricError(f"ERiC-Initialisierung fehlgeschlagen ({code}).", code)
        self._lib = lib
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._lib is not None:
            self._lib.EricBeende()
            self._lib = None

    @staticmethod
    def _declare(lib: ctypes.CDLL) -> None:
        buffer = ctypes.c_void_p
        lib.EricInitialisiere.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        lib.EricInitialisiere.restype = ctypes.c_int
        lib.EricBeende.argtypes = []
        lib.EricBeende.restype = ctypes.c_int
        lib.EricRueckgabepufferErzeugen.argtypes = []
        lib.EricRueckgabepufferErzeugen.restype = buffer
        lib.EricRueckgabepufferInhalt.argtypes = [buffer]
        lib.EricRueckgabepufferInhalt.restype = ctypes.c_char_p
        lib.EricRueckgabepufferFreigeben.argtypes = [buffer]
        lib.EricRueckgabepufferFreigeben.restype = ctypes.c_int
        lib.EricHoleFehlerText.argtypes = [ctypes.c_int, buffer]
        lib.EricHoleFehlerText.restype = ctypes.c_int
        lib.EricBearbeiteVorgang.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.POINTER(_PrintParameters),
            ctypes.POINTER(_CryptoParameters),
            buffer,
            buffer,
        ]
        lib.EricBearbeiteVorgang.restype = ctypes.c_int
        lib.EricGetHandleToCertificate.argtypes = [
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_char_p,
        ]
        lib.EricGetHandleToCertificate.restype = ctypes.c_int
        lib.EricCloseHandleToCertificate.argtypes = [ctypes.c_uint32]
        lib.EricCloseHandleToCertificate.restype = ctypes.c_int
        lib.EricPruefeSteuernummer.argtypes = [ctypes.c_char_p]
        lib.EricPruefeSteuernummer.restype = ctypes.c_int
        lib.EricMakeElsterStnr.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            buffer,
        ]
        lib.EricMakeElsterStnr.restype = ctypes.c_int

    @property
    def lib(self) -> ctypes.CDLL:
        if self._lib is None:
            raise EricError("ERiC ist nicht initialisiert.")
        return self._lib

    def _read_buffer(self, handle: int) -> str:
        content = self.lib.EricRueckgabepufferInhalt(handle)
        return content.decode("utf-8", "replace") if content else ""

    def error_text(self, code: int) -> str:
        handle = self.lib.EricRueckgabepufferErzeugen()
        try:
            self.lib.EricHoleFehlerText(code, handle)
            return self._read_buffer(handle) or f"ERiC-Fehler {code}"
        finally:
            self.lib.EricRueckgabepufferFreigeben(handle)

    def check_tax_number(self, elster_tax_number: str) -> int:
        """``ERIC_OK`` (0), wenn die 13-stellige ELSTER-Steuernummer gültig ist."""
        return self.lib.EricPruefeSteuernummer(elster_tax_number.encode("ascii"))

    def make_elster_tax_number(self, tax_number: str, tax_office_number: str) -> str:
        handle = self.lib.EricRueckgabepufferErzeugen()
        try:
            code = self.lib.EricMakeElsterStnr(
                tax_number.encode("utf-8"), None, tax_office_number.encode("ascii"), handle
            )
            if code != ERIC_OK:
                raise EricError(self.error_text(code), code)
            return self._read_buffer(handle)
        finally:
            self.lib.EricRueckgabepufferFreigeben(handle)

    def process(
        self,
        payload_xml: str,
        datenart_version: str,
        *,
        send: bool,
        certificate_path: str | None = None,
        pin: str | None = None,
        pdf_path: str | None = None,
    ) -> EricResult:
        """Prüft (``send=False``) oder prüft und sendet den Datensatz authentifiziert."""
        flags = ERIC_VALIDIERE
        crypto = None
        certificate_handle = ctypes.c_uint32(0)
        if send:
            if not certificate_path or pin is None:
                raise EricError("Zum Senden sind Zertifikat und PIN erforderlich.")
            flags |= ERIC_SENDE
            pin_support = ctypes.c_uint32(0)
            code = self.lib.EricGetHandleToCertificate(
                ctypes.byref(certificate_handle),
                ctypes.byref(pin_support),
                str(certificate_path).encode("utf-8"),
            )
            if code != ERIC_OK:
                raise EricError(self.error_text(code), code)
            crypto = _CryptoParameters(
                version=_CRYPTO_PARAMETER_VERSION,
                zertifikatHandle=certificate_handle.value,
                pin=pin.encode("utf-8"),
            )
        print_parameters = None
        if pdf_path:
            flags |= ERIC_DRUCKE
            print_parameters = _PrintParameters(
                version=_PRINT_PARAMETER_VERSION,
                vorschau=0,
                duplexDruck=0,
                pdfName=str(pdf_path).encode("utf-8"),
            )

        response = self.lib.EricRueckgabepufferErzeugen()
        server_response = self.lib.EricRueckgabepufferErzeugen()
        try:
            code = self.lib.EricBearbeiteVorgang(
                payload_xml.encode("utf-8"),
                datenart_version.encode("ascii"),
                flags,
                ctypes.byref(print_parameters) if print_parameters else None,
                ctypes.byref(crypto) if crypto else None,
                response,
                server_response,
            )
            response_xml = self._read_buffer(response)
            server_response_xml = self._read_buffer(server_response)
        finally:
            self.lib.EricRueckgabepufferFreigeben(response)
            self.lib.EricRueckgabepufferFreigeben(server_response)
            if certificate_handle.value:
                self.lib.EricCloseHandleToCertificate(certificate_handle.value)

        return EricResult(
            code=code,
            message=self.error_text(code),
            response_xml=response_xml,
            server_response_xml=server_response_xml,
            transfer_ticket=parse_transfer_ticket(server_response_xml, response_xml),
            findings=parse_findings(response_xml),
        )
