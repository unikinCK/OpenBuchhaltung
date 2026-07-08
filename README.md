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

## Sicherheit & Upload-Härtung

Die App setzt Security-Header (`X-Content-Type-Options`, `Referrer-Policy`,
`Content-Security-Policy`) und nutzt gehärtete Session-Cookie-Defaults
(`HttpOnly`, `SameSite=Lax`). In HTTPS-Deployments sollte zusätzlich gesetzt werden:

```bash
export SESSION_COOKIE_SECURE=1
```

Beleguploads sind auf PDF/JPG/PNG begrenzt. Die maximale Uploadgröße liegt
standardmäßig bei 10 MiB und kann angepasst werden:

```bash
export DOCUMENT_MAX_UPLOAD_BYTES=10485760
```

## Bank-CSV-Import

Unter **Bank** lassen sich Kontoumsätze als CSV importieren (Spalten-Aliasse:
Buchungstag/Datum, Betrag — auch deutsches Format `1.234,56` —, Verwendungszweck,
Auftraggeber/Empfänger; Trennzeichen `,` oder `;`). Re-Importe werden dedupliziert.

Offene Umsätze können entweder einer **vorhandenen Buchung zugeordnet** werden
(Vorschläge per Betrags-Matching auf dem Bankkonto) oder **direkt verbucht** werden:
Gegenkonto wählen, optional Steuercode — der Bruttobetrag wird dann automatisch in
Netto + Steuer zerlegt. Beispiel-CSV: `data/demo/bank_demo.csv`.

## Offene Posten (OPOS)

Unter **OPOS** lassen sich debitorische und kreditorische offene Posten erfassen,
optional mit Buchung verknüpfen und vollständig oder teilweise ausgleichen. Ein
Ausgleich kann zusätzlich mit einem Bankumsatz oder einer Zahlungsbuchung verknüpft
werden; die Aktion wird im Audit-Log protokolliert.

## Perioden & Jahresabschluss

Unter **Perioden** in der Navigation lassen sich Buchungsperioden sperren
(Schreibrollen) und entsperren (nur Admin). Der **Jahresabschluss** (nur Admin)
bucht zunächst den **Ergebnisvortrag** (die GuV-Konten werden gegen das
Gewinnvortragskonto glattgestellt — SKR03 `0860`, SKR04 `2970`) und sperrt dann
alle Perioden des Geschäftsjahres; in abgeschlossene Jahre kann nicht mehr
gebucht werden. Alle Aktionen werden im Audit-Log protokolliert.

## E-Rechnung importieren (XRechnung / ZUGFeRD)

Auf der Seite **E-Rechnung** lässt sich eine strukturierte Rechnung (XML) hochladen
und direkt als Eingangsrechnung verbuchen. Unterstützt werden beide in Deutschland
relevanten Syntaxen:

- **XRechnung (UBL)** — `Invoice` im OASIS-UBL-Format
- **XRechnung (CII) / ZUGFeRD** — `CrossIndustryInvoice` im UN/CEFACT-Format

Der Parser liest Rechnungsnummer, Datum, Lieferant sowie Netto-, Steuer- und
Bruttobetrag aus und bucht: Netto auf das gewählte Aufwandskonto (Soll), Steuer auf
das Steuerkonto des gewählten Steuercodes (Soll) und Brutto auf das Kreditorenkonto
(Haben). Das XML wird als Beleg gespeichert und mit der Buchung verknüpft.
Beispieldateien: `data/demo/erechnung_ubl.xml`, `data/demo/erechnung_cii.xml`.

Umgekehrt lässt sich auf derselben Seite eine **Ausgangsrechnung als E-Rechnung
erzeugen** (Käufer + Positionen erfassen, Format wählen) und als XRechnung (UBL)
oder ZUGFeRD/CII herunterladen. Die Verkäuferstammdaten stammen aus der Gesellschaft
und den `SELLER_*`-Umgebungsvariablen (`SELLER_STREET`, `SELLER_POSTAL_CODE`,
`SELLER_CITY`, `SELLER_VAT_ID`, …). Beträge und Steueraufteilung werden aus den
Positionen berechnet.

## DATEV-Export (Buchungsstapel)

Auf der **Berichte**-Seite steht der Download **DATEV-Buchungsstapel (EXTF)**
zur Verfügung (auch als API: `GET /api/v1/exports/datev.csv?company_id=…`).

Die Datei folgt dem EXTF-Format (Kategorie 21, Buchungsstapel): Kopfzeile mit
Metadaten, Spaltenüberschriften und Buchungssätze, kodiert in Windows-1252.
Buchungen mit genau einer Soll- und einer Habenzeile werden als
Konto/Gegenkonto-Satz exportiert; mehrzeilige Buchungen (z. B. mit USt-Zeile)
als Splitbuchung — eine Zeile je Position, gruppiert über Belegfeld 1
(Buchungsnummer). Berater-/Mandantennummer sind über `DATEV_CONSULTANT_NUMBER`
bzw. `DATEV_CLIENT_NUMBER` konfigurierbar.

Der Export ist DATEV-kompatibel, aber nicht zertifiziert: eine
Steuerautomatik über BU-Schlüssel wird nicht gesetzt, da die Umsatzsteuer
bereits als eigene Buchungszeile geführt wird.

## Steuercodes (USt/VSt)

`seed-demo` legt Standard-Steuercodes je Gesellschaft an: `USt19`, `USt7`, `VSt19`, `VSt7`, `frei`.
In der Buchungsmaske wird der Betrag einer Zeile mit Steuercode als **Netto** interpretiert;
die Steuerzeile (z. B. auf 1776 Umsatzsteuer 19 %) wird automatisch ergänzt.

Beispiel Ausgangsrechnung: Forderungen 1.190 € (Soll) an Erlöse 1.000 € (Haben, `USt19`)
→ System bucht zusätzlich 190 € Umsatzsteuer (Haben).

## REST API (API-First)

Die API ist im Entwicklungsmodus offen. Für geschützten Betrieb entweder einen globalen
Token setzen oder API-Auth erzwingen und Benutzer-Tokens ausgeben — dann erfordern alle
API-Aufrufe (außer `/health`) den Header `Authorization: Bearer <token>`:

```bash
export API_AUTH_TOKEN="mein-geheimer-token"
# oder ohne globalen Token:
export API_REQUIRE_AUTH=1
```

Benutzer-Token erzeugen/rotieren:
```bash
flask --app run.py set-api-token --username maria
```

Benutzer-Tokens übernehmen die Rolle und den Tenant-Scope des Benutzers: globale Admins
sehen alle Mandanten, tenantgebundene Benutzer nur ihren Mandanten, Prüfer haben API-seitig
Lesezugriff.

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

## Beleg-OCR & Buchungsvorschlag

Unter **Beleg-OCR** lässt sich ein Beleg (PDF/JPG/PNG) hochladen, automatisch auslesen
und als Eingangsrechnung vorbuchen:

1. **Textgewinnung:** PDFs mit Textebene werden lokal ausgelesen (ohne Fremdbibliothek,
   nur `zlib`), reine Textdateien direkt dekodiert. Für Bild-Belege und gescannte PDFs
   ohne Textebene wird – falls konfiguriert – ein externer OCR-Endpoint verwendet:

   ```bash
   export RECEIPT_OCR_ENDPOINT_URL="http://localhost:11434/v1/responses"
   export RECEIPT_OCR_MODEL="gpt-4.1-mini"
   ```

   Ohne gesetzte `RECEIPT_OCR_*`-Variablen fällt die OCR auf `DOCUMENT_LLM_ENDPOINT_URL`
   zurück. Ist gar kein Endpoint konfiguriert, funktioniert die Pipeline weiterhin für
   PDFs mit Textebene; Bild-Belege werden verständlich abgewiesen.
2. **Analyse (regelbasiert):** Eine deterministische Heuristik erkennt Bruttobetrag,
   Nettobetrag, Steuerbetrag und Steuersatz, Rechnungsdatum, Rechnungsnummer und
   Lieferant und vervollständigt fehlende Beträge rechnerisch (z. B. Netto/Steuer aus
   Brutto + Satz).
3. **KI-Unterstützung & -Kontrolle (optional):** Ist ein LLM-Endpoint konfiguriert,
   extrahiert zusätzlich ein Sprachmodell die Belegfelder strukturiert (als JSON):

   ```bash
   export RECEIPT_LLM_ENDPOINT_URL="http://localhost:11434/v1/responses"
   export RECEIPT_LLM_MODEL="gpt-4.1-mini"
   ```

   Das Ergebnis wird zweifach genutzt:
   - **Unterstützung/Fallback:** Felder, die die Heuristik nicht erkennt, werden aus
     dem LLM ergänzt und anschließend rechnerisch konsolidiert (Status *ergänzt (KI)*).
   - **Kontrolle:** Stimmt der regelbasierte Bruttobetrag mit dem LLM überein, gilt der
     Vorschlag als *bestätigt* (höhere Zuverlässigkeit); weicht er ab, wird eine
     Warnung angezeigt (*Abweichung*, niedrige Zuverlässigkeit).

   Der LLM-Aufruf ist **nicht-blockierend**: bei Fehlern bleibt der regelbasierte
   Vorschlag erhalten und der Fehler wird nur als Warnung vermerkt. Ohne gesetzte
   `RECEIPT_LLM_*`-Variablen fällt die Kontrolle auf `DOCUMENT_LLM_ENDPOINT_URL` zurück;
   ist gar kein Endpoint konfiguriert, arbeitet die Pipeline rein regelbasiert.
4. **Vorschlag & Buchung:** Die erkannten Felder werden angezeigt (inkl. KI-Kontroll-
   Status) und als editierbarer Buchungsvorschlag vorbelegt (Netto → Aufwandskonto,
   Vorsteuer → Steuerkonto, Brutto → Kreditor). Nach Freigabe wird gebucht und der
   gespeicherte Beleg mit der Buchung verknüpft. Alle Schritte werden als Audit-Events
   (`ocr_analyzed` mit `control_status`, `ocr_booked`) protokolliert.

## End-to-End-Kernflows

Ausführen der E2E-Suite lokal:

```bash
pytest -m e2e
```

## Performance-Baseline

Für einen schnellen Profiling-Smoke-Test mit synthetischen Journaldaten:

```bash
pytest -m performance
```

Der Test läuft Reports, OPOS-Liste und Bank-Matching gegen größere Datenmengen und
schützt die zentralen Query-Pfade vor groben Performance-Regressionen.


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

## MCP-Server (API als Tools)

Zusätzlich zum Bridge-Endpunkt gibt es einen eigenständigen **MCP-Server**, der jeden
REST-Endpunkt aus `/api/v1` als MCP-Tool bereitstellt (`health`, `list_companies`,
`create_tenant_with_company`, `create_account`, `create_journal_entry`,
`get_trial_balance`, `get_income_statement`, `get_balance_sheet` sowie die drei
CSV-Exporte). So können MCP-fähige Clients (z. B. Claude Desktop) direkt buchen und
auswerten. Der Server spricht JSON-RPC 2.0 über stdio und benötigt keine zusätzlichen
Abhängigkeiten.

Er ist ein HTTP-Client der laufenden OpenBuchhaltung-Instanz; Basis-URL und Token werden
per Umgebungsvariable gesetzt:

```bash
export OPENBUCHHALTUNG_API_URL="http://localhost:8000/api/v1"   # Standard: http://localhost:5000/api/v1
export OPENBUCHHALTUNG_API_TOKEN="obk_..."                       # optional, falls API-Auth aktiv
python -m app.services.mcp_server
```

### Transport 1: stdio

Für Clients, die den MCP-Server als Subprozess starten (z. B. Claude Desktop, siehe
Beispiel unten), spricht `python -m app.services.mcp_server` JSON-RPC über stdio.

### Transport 2: Streamable HTTP

Alternativ steht derselbe Server über HTTP bereit (`POST /mcp`). Je nach `Accept`-Header
antwortet er mit `application/json` oder als `text/event-stream` (SSE):

```bash
export MCP_HTTP_HOST=127.0.0.1     # Standard 127.0.0.1
export MCP_HTTP_PORT=8080          # Standard 8080
export MCP_HTTP_PATH=/mcp          # Standard /mcp
python -m app.services.mcp_http
```

```bash
# JSON-Antwort
curl -X POST http://127.0.0.1:8080/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# SSE-Stream
curl -N -X POST http://127.0.0.1:8080/mcp \
  -H 'Content-Type: application/json' -H 'Accept: text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_companies","arguments":{}}}'
```

Der Server bindet standardmäßig nur an `127.0.0.1`. Für browserbasierte Clients lässt sich
per `MCP_HTTP_ALLOWED_ORIGINS` (kommagetrennt) eine Origin-Allowlist setzen; Requests mit
nicht erlaubtem `Origin` werden mit 403 abgelehnt (DNS-Rebinding-Schutz). Clients ohne
`Origin`-Header (Desktop/CLI) sind stets zugelassen.

Beispiel-Eintrag für einen MCP-Client (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "openbuchhaltung": {
      "command": "python",
      "args": ["-m", "app.services.mcp_server"],
      "env": {
        "OPENBUCHHALTUNG_API_URL": "http://localhost:8000/api/v1",
        "OPENBUCHHALTUNG_API_TOKEN": "obk_..."
      }
    }
  }
}
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
