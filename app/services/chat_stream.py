"""Chat-Züge mit Live-Fortschritt als NDJSON-Stream (UI und REST-API)."""

from __future__ import annotations

import json
import logging
import queue
import threading
from typing import Any, Callable

from flask import Response, current_app, jsonify, request

from app.services.chat import ChatCancelled, ChatError
from app.services.chat_llm import ChatLLMError
from app.services.internal_api import internal_api_base_url

logger = logging.getLogger(__name__)

STREAM_MIMETYPE = "application/x-ndjson"
# Lebenszeichen im Stream: hält Proxys offen und lässt einen Abbruch des Clients
# (geschlossene Verbindung) spätestens nach dieser Zeit beim Schreiben auffallen.
STREAM_KEEPALIVE_SECONDS = 5.0

Job = Callable[[Callable[[dict[str, Any]], None]], dict[str, Any]]


def wants_stream() -> bool:
    return request.accept_mimetypes.best == STREAM_MIMETYPE


def run_job(job: Job, *, error_status: int = 400):
    """Chat-Zug ohne Fortschritt ausführen (eine JSON-Antwort)."""
    try:
        return jsonify(job(lambda _event: None))
    except ChatError as exc:
        return jsonify({"error": str(exc)}), error_status
    except ChatLLMError as exc:
        return jsonify({"error": str(exc)}), 502


def stream_job(job: Job, *, error_status: int = 400) -> Response:
    """Chat-Zug im Hintergrund ausführen und Fortschritt als NDJSON streamen.

    Jede Zeile ist ein JSON-Objekt: ``progress`` (Zwischenstand), ``ping``,
    abschließend ``done`` (mit ``result``), ``error`` oder ``cancelled``. Schließt
    der Client die Verbindung (Abbrechen), bricht der Zug vor dem nächsten Schritt
    ab; ein bereits laufender Modell- oder Tool-Aufruf wird noch zu Ende geführt.
    Der Job läuft in einem eigenen Thread: Angaben aus dem Request (Benutzer,
    Formulardaten) muss er vorher auslesen. Die Basis-URL des Requests reicht der
    Thread an die Tool-Aufrufe weiter (Download-Links auf die richtige Adresse).
    """
    events: queue.Queue = queue.Queue()
    cancelled = threading.Event()
    app = current_app._get_current_object()
    base_url = request.url_root

    def report(event: dict[str, Any]) -> None:
        if cancelled.is_set():
            raise ChatCancelled()
        events.put({"type": "progress", "event": event})

    def worker() -> None:
        with app.app_context(), internal_api_base_url(base_url):
            try:
                events.put({"type": "done", "result": job(report)})
            except ChatCancelled:
                events.put({"type": "cancelled"})
            except ChatError as exc:
                events.put({"type": "error", "error": str(exc), "status": error_status})
            except ChatLLMError as exc:
                events.put({"type": "error", "error": str(exc), "status": 502})
            except Exception:  # noqa: BLE001 – Fehler gehören in den Stream, nicht ins Nichts
                logger.exception("KI-Chat: Fehler im Hintergrund-Zug")
                events.put({"type": "error", "error": "Interner Fehler.", "status": 500})

    thread = threading.Thread(target=worker, name="chat-turn", daemon=True)
    thread.start()

    def generate():
        try:
            while True:
                try:
                    event = events.get(timeout=STREAM_KEEPALIVE_SECONDS)
                except queue.Empty:
                    event = {"type": "ping"}
                yield json.dumps(event, ensure_ascii=False) + "\n"
                if event["type"] in {"done", "error", "cancelled"}:
                    return
        finally:
            # Ende oder Verbindungsabbruch: laufenden Zug beim nächsten Schritt stoppen.
            cancelled.set()

    response = Response(generate(), mimetype=STREAM_MIMETYPE)
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"  # nginx: nicht puffern
    return response


def respond(job: Job, *, error_status: int = 400):
    """Stream, wenn der Client ``Accept: application/x-ndjson`` sendet, sonst JSON.

    ``error_status`` ist der Status für fachliche Fehler (``ChatError``): die UI
    meldet 400, die REST-API wie ihre übrigen Validierungsfehler 422.
    """
    if wants_stream():
        return stream_job(job, error_status=error_status)
    return run_job(job, error_status=error_status)
