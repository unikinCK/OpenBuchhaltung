# OpenBuchhaltung
Webbasierte Open Source Buchhaltungssoftware.

## Planung
- Umsetzungsplan: `docs/umsetzungsplan.md`

## Schnellstart
1. Virtuelle Umgebung erstellen und aktivieren
2. Abhängigkeiten installieren
   ```bash
   pip install -r requirements-dev.txt
   ```
3. Demo-Daten anlegen (Mandant, SKR03, Steuercodes, Benutzer, Beispielbuchungen)
   ```bash
   flask --app run.py seed-demo
   ```
4. Anwendung starten
   ```bash
   python run.py
   ```
   Die App läuft auf Port **8000** (macOS reserviert Port 5000 für AirPlay).
   Anderer Port: `PORT=5001 python run.py`
5. Im Browser anmelden: http://localhost:8000

Tests und Linting:
```bash
ruff check .
pytest
```

Optional mit Containern:
```bash
docker compose up --build
```

## Login & Benutzer

Alle UI-Seiten erfordern eine Anmeldung. `seed-demo` legt folgende Benutzer an:

| Benutzer     | Passwort         | Rolle      | Zugriff |
|--------------|------------------|------------|---------|
| `admin`      | `admin123`       | Admin      | alle Mandanten |
| `buchhalter` | `buchhalter123`  | Buchhalter | nur Demo Mandant |
| `pruefer`    | `pruefer123`     | Prüfer     | nur lesen |

Weitere Benutzer per CLI:
```bash
flask --app run.py create-user --username maria --password geheim --role Buchhalter --tenant-id 1
```

Benutzer mit `--tenant-id` sehen nur Daten ihres Mandanten; ohne Angabe haben sie globalen Zugriff.

## Bank-CSV-Import

Unter **Bank** lassen sich Kontoumsätze als CSV importieren (Spalten-Aliasse:
Buchungstag/Datum, Betrag — auch deutsches Format `1.234,56` —, Verwendungszweck,
Auftraggeber/Empfänger; Trennzeichen `,` oder `;`). Re-Importe werden dedupliziert.

Offene Umsätze können entweder einer **vorhandenen Buchung zugeordnet** werden
(Vorschläge per Betrags-Matching auf dem Bankkonto) oder **direkt verbucht** werden:
Gegenkonto wählen, optional Steuercode — der Bruttobetrag wird dann automatisch in
Netto + Steuer zerlegt. Beispiel-CSV: `data/demo/bank_demo.csv`.

## Perioden & Jahresabschluss

Unter **Perioden** in der Navigation lassen sich Buchungsperioden sperren
(Schreibrollen) und entsperren (nur Admin). Der **Jahresabschluss** (nur Admin)
sperrt alle Perioden des Geschäftsjahres; in abgeschlossene Jahre kann nicht
mehr gebucht werden. Alle Aktionen werden im Audit-Log protokolliert.

## Steuercodes (USt/VSt)

`seed-demo` legt Standard-Steuercodes je Gesellschaft an: `USt19`, `USt7`, `VSt19`, `VSt7`, `frei`.
In der Buchungsmaske wird der Betrag einer Zeile mit Steuercode als **Netto** interpretiert;
die Steuerzeile (z. B. auf 1776 Umsatzsteuer 19 %) wird automatisch ergänzt.

Beispiel Ausgangsrechnung: Forderungen 1.190 € (Soll) an Erlöse 1.000 € (Haben, `USt19`)
→ System bucht zusätzlich 190 € Umsatzsteuer (Haben).

## REST API (API-First)

Die API ist im Entwicklungsmodus offen. Für geschützten Betrieb einen Token setzen —
dann erfordern alle API-Aufrufe (außer `/health`) den Header `Authorization: Bearer <token>`:

```bash
export API_AUTH_TOKEN="mein-geheimer-token"
```

Vollwertige API-Tokens je Benutzer folgen in Phase 3.

Basis-Endpunkte:

- `GET /api/v1/health`
- `POST /api/v1/tenants` (legt Mandant + Gesellschaft an)
- `GET /api/v1/companies`
- `POST /api/v1/accounts`
- `POST /api/v1/journal-entries` (mehrzeilige Buchung, Validierung mit 422-Details)
- `GET /api/v1/trial-balance`

Beispiel:
```bash
curl -X POST http://localhost:8000/api/v1/tenants \
  -H "Content-Type: application/json" \
  -d '{"tenant_name":"Mandant A","company_name":"Mandant A GmbH","currency_code":"EUR"}'
```

Journalbuchung (mehrzeilig, optional mit `tax_code_id` je Zeile für automatische USt-Buchung):
```bash
curl -X POST http://localhost:8000/api/v1/journal-entries \
  -H "Content-Type: application/json" \
  -d '{
    "company_id": 1,
    "entry_date": "2026-04-04",
    "description": "Rechnung 1001",
    "status": "posted",
    "lines": [
      {"account_id": 1, "debit_amount": "80.00", "credit_amount": "0.00", "description": "Teilbetrag"},
      {"account_id": 2, "debit_amount": "20.00", "credit_amount": "0.00", "description": "Nebenkosten"},
      {"account_id": 3, "debit_amount": "0.00", "credit_amount": "100.00", "description": "Umsatzerlös"}
    ]
  }'
```

Validierungsfehler liefern `422` mit feldbezogenen Details:
```json
{
  "error": "Validation failed.",
  "details": [
    {"field": "journal_entry", "message": "Zeile 2: Betrag muss größer 0 sein."}
  ]
}
```

## Dokument-Upload mit optionalem LLM-Update

Wenn ein externer OpenAI-Responses-kompatibler Endpoint konfiguriert ist, wird beim Belegupload
zusätzlich ein nicht-blockierender LLM-Request ausgeführt:

```bash
export DOCUMENT_LLM_ENDPOINT_URL="http://localhost:11434/v1/responses"
export DOCUMENT_LLM_MODEL="gpt-4.1-mini"
```

Bei LLM-Fehlern bleibt der Upload erfolgreich; der Fehler wird als Audit-Event protokolliert.

## End-to-End-Kernflows

Ausführen der E2E-Suite lokal:

```bash
pytest -m e2e
```


## UI-Screenshot Tool

Für schnelle UI-Checks gibt es ein Screenshot-Skript:

```bash
python tools/screenshot_ui.py --url http://127.0.0.1:8000/ --output artifacts/ui-home.png
```

Einmalig Browser-Binaries installieren:

```bash
python -m playwright install chromium
```

## LLM/MCP Kommunikation

Es gibt einen MCP-Bridge-Endpunkt:

- `POST /api/v1/mcp/call`

Die App leitet JSON-RPC-Aufrufe an einen konfigurierten MCP-Server weiter.

Konfiguration:
```bash
export MCP_SERVER_URL="http://localhost:8080/mcp"
```

Beispiel:
```bash
curl -X POST http://localhost:8000/api/v1/mcp/call \
  -H "Content-Type: application/json" \
  -d '{"id":"1","method":"tools/list","params":{}}'
```

## Kontenrahmenimport (SKR03/SKR04)

CSV-Import per Flask-CLI (idempotent, Duplikate werden uebersprungen).

Vorgebundene Kontenrahmen aus dem Repo importieren:

```bash
flask --app run.py import-kontenrahmen --company-id 1 --chart skr03
flask --app run.py import-kontenrahmen --company-id 1 --chart skr04
```

Alternativ eigene CSV-Datei importieren:

```bash
flask --app run.py import-kontenrahmen --company-id 1 --csv-path ./mein_kontenrahmen.csv
```

Hinweis: Genau eine Quelle muss angegeben werden (`--chart` oder `--csv-path`).

Unterstuetzte Kopfzeilen (Alias):
- `code` oder `Kontonummer`
- `name` oder `Bezeichnung`
- `account_type` oder `Kontoart`

Fehlerhafte Zeilen werden protokolliert und brechen den Gesamtimport nicht ab.
