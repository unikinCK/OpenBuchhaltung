from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(slots=True)
class DocumentLLMError(Exception):
    message: str


def send_document_update(
    *,
    endpoint_url: str | None,
    model: str,
    company_id: int,
    document_id: int,
    file_name: str,
    mime_type: str,
    journal_entry_id: int | None,
) -> dict:
    if not endpoint_url:
        raise DocumentLLMError("LLM endpoint URL is not configured.")

    payload = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Du bist ein Assistent für Belegmetadaten-Updates in einer "
                            "deutschen Buchhaltungsanwendung."
                        ),
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "Dokument wurde hochgeladen. Bitte antworte mit einer kurzen JSON-"
                            "Zusammenfassung mit Feldern suggestion, confidence und notes. "
                            f"company_id={company_id}, document_id={document_id}, "
                            f"file_name={file_name}, mime_type={mime_type}, "
                            f"journal_entry_id={journal_entry_id}."
                        ),
                    }
                ],
            },
        ],
        "metadata": {
            "source": "openbuchhaltung-document-upload",
            "company_id": str(company_id),
            "document_id": str(document_id),
        },
    }

    request = Request(
        endpoint_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=20) as response:
            response_body = response.read().decode("utf-8")
            return json.loads(response_body)
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise DocumentLLMError(f"LLM endpoint returned HTTP {exc.code}: {error_body}") from exc
    except URLError as exc:
        raise DocumentLLMError("LLM endpoint is not reachable.") from exc
    except json.JSONDecodeError as exc:
        raise DocumentLLMError("LLM endpoint response is not valid JSON.") from exc
