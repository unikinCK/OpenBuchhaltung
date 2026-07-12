# OpenBuchhaltung

OpenBuchhaltung ist eine webbasierte Open-Source-Buchhaltung für deutsche
Unternehmen, die ihre Finanzdaten nachvollziehbar, automatisierbar und ohne
Blackbox verwalten möchten.

Der Fokus liegt auf der doppelten Buchführung für Kapitalgesellschaften
(UG, GmbH, gGmbH), sauberer Mandantentrennung, prüfbaren Buchungsprozessen und
offenen Schnittstellen. Die Anwendung verbindet klassische Buchhaltungsabläufe
mit modernen Workflows: Belege hochladen, Buchungen erfassen, Bankumsätze
abgleichen, Umsatzsteuer vorbereiten, Reports erzeugen und Daten per API oder
MCP weiterverarbeiten.

OpenBuchhaltung versteht sich als transparentes Werkzeug statt als Blackbox:
Teams, Entwicklerinnen, Gründer und Steuerkanzleien sollen nachvollziehen
können, was gebucht, exportiert und protokolliert wird.

## Was kann OpenBuchhaltung?

- **Kernbuchhaltung:** Mandanten, Gesellschaften, Konten, Steuercodes,
  mehrzeilige Journalbuchungen, Storno und Festschreibung.
- **Deutsche Praxis:** SKR03/SKR04-Import, Umsatzsteuer-/Vorsteuerlogik,
  UStVA-Berechnung und festhaltbare Voranmeldungs-Snapshots.
- **Belege & E-Rechnung:** Uploads, optionale OCR-/LLM-Unterstützung,
  XRechnung/ZUGFeRD-Import und E-Rechnungs-Export.
- **Bank & OPOS:** CSV-Bankimport, Deduplizierung, Matching,
  offene Posten und Zahlungsausgleich.
- **Anlagenbuchhaltung:** Anlagegüter, AfA-Pläne, GWG, Sammelposten,
  außerplanmäßige Abschreibung und Anlagenabgang.
- **Auswertungen & Exporte:** Summen-/Saldenliste, GuV, Bilanz,
  Journalexporte und DATEV-kompatibler Buchungsstapel.
- **Schnittstellen:** REST API mit Token-Auth, MCP-Server für agentische
  Workflows und ein ELSTER-Bridge-Konzept über lokalen ERiC-Runner.
- **Nachvollziehbarkeit:** Rollen, Tenant-Scoping, Audit-Log,
  Security-Header und Migrationsstrategie für reproduzierbare Deployments.

## Für wen ist das interessant?

- kleine Kapitalgesellschaften, die eine nachvollziehbare eigene Buchhaltung
  aufbauen oder verstehen möchten
- Steuerkanzleien und Buchhaltungsteams, die offene Schnittstellen und
  reproduzierbare Exporte brauchen
- Entwicklerinnen und Automatisierer, die Buchhaltungsprozesse per API/MCP
  in eigene Workflows einbinden wollen
- Open-Source-Beitragende mit Interesse an deutscher Buchhaltung,
  Compliance-Basisarbeit und praktischer Finanzsoftware

## Projektstatus

OpenBuchhaltung ist ein aktives Entwicklungsprojekt. Viele Kernflows sind
bereits umgesetzt und automatisiert getestet, trotzdem ersetzt das Projekt noch
keine fachliche Prüfung durch Steuerberatung oder Wirtschaftsprüfung.

Wichtig zur Einordnung:

- DATEV-Export ist kompatibel angelegt, aber nicht DATEV-zertifiziert.
- GoBD-Funktionen sind als technische Basis umgesetzt, aber keine formale
  GoBD-/IDW-PS-880-Zertifizierung.
- ELSTER ist app-seitig vorbereitet; produktive Übermittlung benötigt eine
  lokale ERiC-Bibliothek, ein ELSTER-Zertifikat und einen passenden Runner.

## Planung & Dokumentation

- Umsetzungsplan: `docs/umsetzungsplan.md`
- Compliance-Dokumente: `docs/compliance/`
- Architekturentscheidungen: `docs/adr/`

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

### Datenbank & Migrationen

Beim Start bringt die App die Datenbank automatisch auf den aktuellen Stand:
- **Leere DB:** Das Schema wird angelegt und auf den Alembic-Head gestampt.
- **Bestehende, von der App verwaltete DB:** Ausstehende Migrationen werden per
  `alembic upgrade head` automatisch nachgezogen — ein **Redeploy gegen eine
  bestehende Datenbank** wendet neue Migrationen also selbst an (kein manueller Schritt
  nötig). Schlägt eine Migration fehl, bricht der Start bewusst ab (Fail-fast), statt
  später mit Schema-Fehlern zu laufen.
- **Bestehende DB ohne Alembic-Verwaltung** (kein `alembic_version`) wird nicht
  angefasst; hier ist Migration manuell durchzuführen.

Manuell migrieren (z. B. für eine externe DB):
```bash
DATABASE_URL="sqlite+pysqlite:///$(pwd)/instance/openbuchhaltung.db" alembic upgrade head
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

## Anlagenbuchhaltung (Anlagenverzeichnis & AfA)

Unter **Anlagen** werden Anlagegüter mit den in HGB und Steuerrecht üblichen
Abschreibeverfahren geführt:

| Verfahren | Rechtsgrundlage | Besonderheit |
|-----------|-----------------|--------------|
| `linear` | § 7 Abs. 1 EStG, § 253 Abs. 3 HGB | im Zugangsjahr zeitanteilig/monatsgenau (§ 7 Abs. 1 S. 4 EStG) |
| `degressive` | § 7 Abs. 2 EStG | geometrisch-degressiv mit automatischem Übergang zur linearen AfA |
| `leistung` | § 7 Abs. 1 S. 6 EStG | Abschreibung nach tatsächlicher Jahresleistung |
| `gwg` | § 6 Abs. 2 EStG | Sofortabschreibung geringwertiger Wirtschaftsgüter (≤ 800 €) |
| `sammelposten` | § 6 Abs. 2a EStG | Poolabschreibung gleichmäßig über 5 Jahre (20 % p. a.) |
| `manuell` | – | kein automatischer Plan, nur außerplanmäßige Buchung |

Restwert und Erinnerungswert (1,00 €) bilden die Buchwert-Untergrenze. Die Seite
zeigt das Anlagenverzeichnis mit aktuellem Buchwert und den vollständigen
**Abschreibungsplan** (Buchwertverlauf je Jahr). Die planmäßige AfA je
Wirtschaftsjahr wird als Direktabschreibung gebucht
(*Soll Abschreibungen an Anlagekonto*); zusätzlich gibt es die **außerplanmäßige
Abschreibung/AfaA** (§ 253 Abs. 3 HGB, § 7 Abs. 1 S. 7 EStG) und den
**Anlagenabgang** (Ausbuchung des Restbuchwerts). Alle Aktionen laufen ins
Audit-Log. REST: `POST/GET /api/v1/fixed-assets`,
`GET /api/v1/fixed-assets/<id>/schedule`,
`POST /api/v1/fixed-assets/<id>/depreciation`.

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

Die API-Authentifizierung ist **standardmäßig aktiv** (default-secure): alle
API-Aufrufe außer `/health` erfordern den Header `Authorization: Bearer <token>`
(globaler `API_AUTH_TOKEN` oder Benutzer-Token). Eingeloggte UI-Sessions erhalten
zusätzlich lesenden API-Zugriff (GET) im eigenen Tenant-Scope — darüber laufen
z. B. die CSV-Downloadlinks der Berichte-Seite.

Nur für lokale Entwicklung lässt sich die API öffnen:

```bash
export API_REQUIRE_AUTH=0   # nicht für Produktion!
```

Optional zusätzlich ein globaler Token:

```bash
export API_AUTH_TOKEN="mein-geheimer-token"
```

Fehlgeschlagene UI-Logins sind rate-limitiert (Default: 5 Versuche je
Benutzername/IP in 15 Minuten, konfigurierbar über `LOGIN_RATE_LIMIT_ATTEMPTS`
und `LOGIN_RATE_LIMIT_WINDOW_SECONDS`; Abschalten mit `LOGIN_RATE_LIMIT=0`).

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
- `GET /api/v1/accounts` — Konten einer Gesellschaft (`company_id`; optional `include_inactive=true`)
- `POST /api/v1/journal-entries` (mehrzeilige Buchung, Validierung mit 422-Details)
- `GET /api/v1/trial-balance` — optional `date_from`/`date_to` (JJJJ-MM-TT)
- `GET /api/v1/income-statement` — optional `date_from`/`date_to` (Zeitraum der GuV)
- `GET /api/v1/balance-sheet` — optional `date_to` (Stichtag; Alias `as_of`)

Die Report-Endpunkte akzeptieren einen **Zeitraum**: GuV und Summen-/Saldenliste
werten Buchungen mit `entry_date` in `[date_from, date_to]` aus, die Bilanz als
Stichtagsbetrachtung bis einschließlich `date_to`. Ohne Angabe werden alle Buchungen
berücksichtigt. Der ausgewertete Zeitraum steht im Feld `period` der Antwort. Dieselben
Parameter stehen auch als MCP-Tool-Argumente (`date_from`/`date_to`) zur Verfügung.

```bash
curl "http://localhost:8000/api/v1/income-statement?company_id=1&date_from=2026-01-01&date_to=2026-03-31"
```

Beispiel:
```bash
curl -X POST http://localhost:8000/api/v1/tenants \
  -H "Content-Type: application/json" \
  -d '{"tenant_name":"Mandant A","company_name":"Mandant A GmbH","currency_code":"EUR"}'
```

Journalbuchung (mehrzeilig, optional mit `tax_code_id` je Zeile für automatische USt-Buchung).
Das Konto je Zeile wird entweder über die interne `account_id` **oder** über die Kontonummer
`account_code` (z. B. `"1200"`) angegeben – die Nummer wird serverseitig zur ID aufgelöst:
```bash
curl -X POST http://localhost:8000/api/v1/journal-entries \
  -H "Content-Type: application/json" \
  -d '{
    "company_id": 1,
    "entry_date": "2026-04-04",
    "description": "Rechnung 1001",
    "status": "posted",
    "lines": [
      {"account_code": "1200", "debit_amount": "80.00", "description": "Teilbetrag"},
      {"account_code": "1200", "debit_amount": "20.00", "description": "Nebenkosten"},
      {"account_code": "8400", "credit_amount": "100.00", "description": "Umsatzerlös"}
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
`create_tenant_with_company`, `create_account`, `list_accounts`, `create_journal_entry`,
`create_fixed_asset`, `list_fixed_assets`, `get_depreciation_schedule`, `post_depreciation`,
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
`Origin`-Header (Desktop/CLI) sind stets zugelassen. Steht `*` in der Allowlist, sind alle
Origins erlaubt — sinnvoll hinter einem vertrauenswürdigen Proxy mit eigenem Zugriffsschutz
(Tailscale Serve, Caddy).

### Transport 2 in Docker Compose

Der Streamable-HTTP-Transport ist als eigener `mcp`-Service in der `docker-compose.yml`
enthalten. Er baut dasselbe Image, spricht die App über das Compose-Netz an
(`OPENBUCHHALTUNG_API_URL=http://app:8000/api/v1`) und ist auf dem Host unter
**Port 8090** (nur an `127.0.0.1` gebunden) erreichbar:

```bash
docker compose up mcp        # startet mcp inkl. Abhängigkeit app
# Test vom Host aus:
curl -X POST http://localhost:8090/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

Bei aktiver API-Authentifizierung (`API_REQUIRE_AUTH=1`) wird der Token über die
Host-Umgebungsvariable `OPENBUCHHALTUNG_API_TOKEN` durchgereicht.

### HTTPS-Zugang für Claude-Desktop-Custom-Connectoren

Claude-Desktop-Custom-Connectoren benötigen eine per **HTTPS** mit gültigem Zertifikat
erreichbare URL. Der `mcp`-Service liefert nur reines HTTP auf `127.0.0.1:8090`; davor
gehört ein TLS-Terminierer.

**Variante A — Tailscale Serve (für `*.ts.net`-Hostnamen, empfohlen im Tailnet).**
Tailscale stellt für den Node automatisch ein gültiges Let's-Encrypt-Zertifikat aus
(vorher in der Tailscale-Admin-Konsole unter *DNS → HTTPS Certificates* aktivieren). Auf
dem Host, auf dem der `mcp`-Container läuft:

```bash
# HTTPS auf 443 -> lokaler MCP-Port 8090
tailscale serve --bg --https=443 http://127.0.0.1:8090
tailscale serve status        # zeigt die aktive URL
```

Der Endpunkt ist dann für Geräte im selben Tailnet unter
`https://<node>.ts.net/mcp` erreichbar (z. B. `https://webbox.tail717550.ts.net/mcp`).
Da der Zugriff bereits durch das Tailnet geschützt ist, empfiehlt sich am `mcp`-Service
`MCP_HTTP_ALLOWED_ORIGINS=*` (falls Claude Desktop einen `Origin`-Header sendet). Kein
Caddy nötig — ein öffentliches Zertifikat für `*.ts.net` lässt sich per HTTP-Challenge
ohnehin nicht ausstellen.

**Variante B — Caddy (für eine öffentliche Domain mit erreichbaren Ports 80/443).**
Enthalten als opt-in `caddy`-Service (Profil `proxy`) samt `Caddyfile`. Domain per
`MCP_DOMAIN` setzen, dann:

```bash
export MCP_DOMAIN=mcp.example.com     # A/AAAA-Record muss auf den Server zeigen
docker compose --profile proxy up -d caddy
```

Caddy holt automatisch ein Let's-Encrypt-Zertifikat und proxyt auf `mcp:8090`; der
`Origin`-Header wird dabei entfernt. Connector-URL: `https://<MCP_DOMAIN>/mcp`.

> **Sicherheit:** Der MCP-Endpunkt selbst hat keine Authentifizierung — wer die URL
> erreicht, kann Tools aufrufen (u. a. Buchungen anlegen). Zugriff daher auf das Tailnet
> bzw. bekannte Client-IPs beschränken (siehe Kommentare im `Caddyfile`) und die
> App-API zusätzlich per `API_REQUIRE_AUTH=1` + Token absichern.

**Connector in Claude Desktop einrichten:** *Einstellungen → Connectors → Custom Connector
hinzufügen* → die HTTPS-URL (`https://…/mcp`) eintragen, dann Claude Desktop neu starten.

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
