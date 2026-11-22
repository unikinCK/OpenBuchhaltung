"""ERiC-Runner: prüft oder sendet einen ElsterXML-Datensatz in einem eigenen Prozess.

Aufruf durch ``EricElsterTransport`` (``python -m app.services.eric_runner``) oder einen eigenen
Befehl aus ``ELSTER_ERIC_COMMAND`` mit demselben Vertrag:

Eingabe (Umgebung)
    ``ELSTER_PAYLOAD_XML``        Pfad zum Datensatz
    ``ELSTER_RESPONSE_JSON``      Pfad für die Antwort
    ``ELSTER_DATENART_VERSION``   z. B. ``UStVA_2026``
    ``ELSTER_ERIC_MODE``          ``send`` (Standard) oder ``validate``
    ``ELSTER_ERIC_LIBRARY_PATH``  ERiC-``lib``-Verzeichnis oder ``libericapi``
    ``ELSTER_ERIC_LOG_DIR``       Verzeichnis für ``eric.log`` (Standard: Arbeitsverzeichnis)
    ``ELSTER_CERTIFICATE_PATH``   Zertifikatsdatei (.pfx), nur zum Senden
    ``ELSTER_CERTIFICATE_PIN``    PIN des Zertifikats, nur zum Senden

Ausgabe (JSON)
    ``status`` (``transmitted``/``validated``/``failed``), ``transfer_ticket``,
    ``response_protocol``, ``eric_code``, ``findings``.

Der Exit-Code ist 0, sobald eine Antwort geschrieben wurde – auch bei fachlichen Fehlern; nur
Aufruffehler (fehlende Umgebung, ERiC nicht ladbar) enden mit 2.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from app.services.eric import EricClient, EricError


def _protocol(result_message: str, findings: list[str], server_response_xml: str) -> str:
    lines = [result_message]
    lines.extend(f"- {finding}" for finding in findings)
    if server_response_xml.strip():
        lines.extend(["", server_response_xml.strip()])
    return "\n".join(lines)


def run(env: dict[str, str]) -> dict[str, object]:
    payload_path = env.get("ELSTER_PAYLOAD_XML")
    datenart_version = env.get("ELSTER_DATENART_VERSION")
    library_path = env.get("ELSTER_ERIC_LIBRARY_PATH")
    if not payload_path or not datenart_version or not library_path:
        raise EricError(
            "ELSTER_PAYLOAD_XML, ELSTER_DATENART_VERSION und ELSTER_ERIC_LIBRARY_PATH sind nötig."
        )
    mode = (env.get("ELSTER_ERIC_MODE") or "send").strip().lower()
    if mode not in {"send", "validate"}:
        raise EricError("ELSTER_ERIC_MODE muss 'send' oder 'validate' sein.")
    send = mode == "send"
    payload_xml = Path(payload_path).read_text(encoding="utf-8")
    log_dir = env.get("ELSTER_ERIC_LOG_DIR") or os.getcwd()

    with EricClient(library_path, log_dir) as client:
        result = client.process(
            payload_xml,
            datenart_version,
            send=send,
            certificate_path=env.get("ELSTER_CERTIFICATE_PATH") if send else None,
            pin=env.get("ELSTER_CERTIFICATE_PIN") if send else None,
        )

    if result.ok:
        status = "transmitted" if send else "validated"
    else:
        status = "failed"
    return {
        "status": status,
        "transfer_ticket": result.transfer_ticket or "",
        "response_protocol": _protocol(
            result.message, result.findings, result.server_response_xml
        ),
        "eric_code": result.code,
        "findings": result.findings,
    }


def main() -> int:
    env = dict(os.environ)
    response_path = env.get("ELSTER_RESPONSE_JSON")
    if not response_path:
        print("ELSTER_RESPONSE_JSON fehlt.", file=sys.stderr)
        return 2
    try:
        response = run(env)
    except (EricError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    Path(response_path).write_text(json.dumps(response, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
