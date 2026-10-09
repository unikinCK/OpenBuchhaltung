# Changelog

Alle nennenswerten Änderungen an OpenBuchhaltung werden hier festgehalten.
Das Format folgt [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), die
Versionierung [SemVer](https://semver.org/lang/de/). Releases tragen den Git-Tag
`v<Version>`; die Version steht in `pyproject.toml` (`[project] version`) und wird
über `GET /api/v1/health` sowie im Prüferexport-Manifest ausgegeben.

## [Unreleased]

### Leistungsdatum und Ergänzen offener Buchungen
- Neu: Buchungen tragen optional ein Leistungsdatum (`journal_entry.service_date`,
  Migration 0044) – Feld in der Buchungsmaske, `service_date` in
  `POST /api/v1/journal-entries` und MCP `create_journal_entry`, Anzeige im Journal und
  Spalte im Journal-CSV.
- UStVA, Jahreserklärung und ZM ordnen danach zu: Umsätze und EU-Leistungen (Kz 21,
  ZM „S“, § 13b Abs. 1 Kz 46/47) im Zeitraum der Leistung, ig. Lieferungen, ig. Erwerb
  und übrige § 13b-Fälle mit der Rechnung (spätestens Ende des Folgemonats), Vorsteuer
  zum späteren Datum aus Leistung und Rechnung. Ohne Leistungsdatum gilt weiter das
  Buchungsdatum. Ein Storno übernimmt das Leistungsdatum; in der ZM heben sich
  Buchung und Storno ohne Partner im selben Zeitraum auf (nicht „fehlend“).
- Neu: Offene (nicht festgeschriebene) Buchungen ergänzen – Leistungsdatum und
  Geschäftspartner auf Sammelkonto-Zeilen; UI-Link „Ergänzen“, API
  `PATCH /api/v1/journal-entries/<id>`, MCP `amend_journal_entry`. Jede Änderung steht
  mit altem und neuem Wert im Audit-Log; festgeschriebene und stornierte Buchungen,
  gesperrte Perioden und abgeschlossene Geschäftsjahre sind ausgeschlossen.
- Inhaltshash Version 4 versiegelt das Leistungsdatum; Siegel der Versionen 2 und 3
  bleiben gültig (kein Re-Hashing).
- DATEV-Export: Mit Leistungsdatum schreibt der Stapel 116 Felder mit Feld 115
  „Leistungsdatum“ und Feld 116 „Datum Zuord. Steuerperiode“; sonst unverändert 14.
- Über die API angelegte Buchungen protokollieren jetzt den API-Benutzer statt
  „system“.

### UStVA: EU-Umsätze, Reverse Charge, ig. Erwerb und Zusammenfassende Meldung

- Erträge ohne Umsatzsteuer ordnet die UStVA über die DATEV-Kontenfunktion des
  Erlöskontos zu: Kz 41 (ig. Lieferung, 8125/4125), 42 (Dreiecksgeschäft), 43 (Ausfuhr,
  § 4 Nr. 2–7), 44, 48 (§ 4 Nr. 8 ff.), 21 (sonstige Leistung an EU-Unternehmer,
  8336/4336), 45 (nicht steuerbar), 60 (§ 13b als Leistender), 87 (0 %). Neue Spalte
  `kennzahl` in `data/kontenrahmen/datev_kontenfunktionen.csv`.
- **Geändert:** Erträge ohne Umsatzsteuer auf Konten ohne UStVA-Funktion (z. B. Zinsen)
  zählen nicht mehr pauschal in Kz 48 (Review F10), sondern erscheinen als Hinweis;
  `GET /api/v1/vat-return` liefert dazu `warnings` und `unassigned`, die UStVA-Seite
  zeigt beides.
- Reverse Charge als Leistungsempfänger und innergemeinschaftlicher Erwerb über die
  Steuerkonten (SKR03 1572/1574, 1577/1578, 1772/1774, 1785/1787; SKR04 1402/1404,
  1407/1408, 3802/3804, 3835/3837): Kz 46/47 bzw. 84/85, 89/93, Vorsteuer 61/67;
  Bemessungsgrundlage aus den Aufwands-/Anlagenzeilen der Buchung (ohne Aufwandszeile
  oder bei zusätzlicher normaler Vorsteuer aus Steuer ÷ Satz), EU-Leistungen über das
  EU-Aufwandskonto (3123/5923) oder einen Partner mit EU-USt-IdNr.; Kz 83 rechnet alle
  Umsatz- gegen alle Vorsteuer.
- Neu: Zusammenfassende Meldung je Kunden-USt-IdNr. und Art (L/D/S) – UI auf der
  UStVA-Seite, API `GET /api/v1/zm`, MCP `get_zm_report`.
- DATEV-Kontenfunktionen liegen jetzt in `app/services/datev_account_functions.py`
  (gemeinsam für DATEV-Export und UStVA).

### DATEV-Export: Rundungscent und Steuerkonten des anderen Kontenrahmens

- Weicht die gebuchte Steuer um wenige Cent von DATEVs Rechnung aus dem Bruttobetrag ab
  (einzeln gerundete Rechnungspositionen, Aufteilung auf mehrere Gegenkonten; höchstens
  5 Cent und 1 % der Steuer), bleibt der Satz brutto mit Automatikkonto bzw.
  Steuerschlüssel; ein Korrektursatz „Steuer-Rundungsdifferenz“ verschiebt die Differenz
  zwischen Konto und DATEV-Steuerkonto. Bisher fiel der ganze Satz auf Netto plus
  eigene Steuerzeile zurück.
- Steuerkonten des jeweils anderen Kontenrahmens (SKR04 1401/1406/3801/3806 in einer
  SKR03-Buchhaltung und umgekehrt) erscheinen im Stapel unter der Nummer des erkannten
  Rahmens; Sätze, die danach Konto und Gegenkonto gleich hätten, entfallen.

### DATEV-Export je Wirtschaftsjahr

- Der Buchungsstapel umfasst genau ein Wirtschaftsjahr bzw. einen Zeitraum darin:
  DATEV liest das Jahr des Belegdatums (TTMM) aus dem WJ-Beginn im Kopffeld 13.
  Bisher enthielt die Datei alle Jahre mit WJ-Beginn 1.1. des ältesten Belegs; ein
  Mehrjahresstapel landete in DATEV im falschen Jahr oder wurde abgelehnt (Review F3).
- Kopffeld 13 ist der tatsächliche WJ-Beginn (auch abweichendes und Rumpf-WJ),
  15/16 der gewählte Zeitraum (Standard: das ganze Wirtschaftsjahr), das
  Festschreibekennzeichen (Feld 21) gilt nur für die exportierten Buchungen.
- **Geändert:** `GET /api/v1/exports/datev.csv` und MCP `export_datev_csv` nehmen
  `fiscal_year_id` sowie optional `date_from`/`date_to`. Ohne Angabe wird das
  Wirtschaftsjahr mit Buchungen exportiert; haben mehrere Wirtschaftsjahre
  Buchungen, antwortet die API mit 400 und listet sie unter `fiscal_years`.
  Ein Zeitraum über die WJ-Grenze ergibt ebenfalls 400. Dateiname mit WJ und
  Zeitraum (`EXTF_Buchungsstapel_1_WJ2026_20260101-20261231.csv`).
- UI: Auf der Berichte-Seite Auswahl des Wirtschaftsjahres (vorgewählt das des
  Auswertungszeitraums bzw. das jüngste mit Buchungen) mit optionalem Von/Bis;
  eine ungültige Auswahl erscheint als Hinweis (`GET /reports/datev.csv`).

### DATEV-Export: Brutto-Prinzip und Gegenkonto

- Der Buchungsstapel folgt dem Brutto-Prinzip, das DATEV beim Import verlangt
  (DATEV-Hilfe Dok.-Nr. 1036228): Steuerzeilen gehen in den Bruttobetrag ihrer
  Bemessungsgrundlage auf – auf Automatikkonten (AM/AV laut DATEV-Kontenrahmen 2026,
  z. B. 8400/4400, SKR03 3400) ohne BU-Schlüssel, sonst mit Steuerschlüssel
  101/102/401/402. Bisher standen der Nettobetrag auf dem Automatikkonto und die Steuer
  in eigener Zeile; DATEV hätte die Steuer ein zweites Mal errechnet.
- Rechnet DATEV aus dem Brutto nicht exakt die gebuchte Steuer (Rundung laut Rechnung,
  Aufteilung auf mehrere Gegenkonten), passt der Steuersatz nicht zur Kontenfunktion oder
  fehlt die Steuerzeile (Abschluss-, Umbuchungen), trägt der Satz auf dem Automatikkonto
  BU 40 (Aufhebung der Automatik) und die Steuer bleibt eigener Satz.
- Jeder Buchungssatz hat Konto und Gegenkonto (DATEV-Muss-Felder, sonst Importfehler
  REW00223); mehrzeilige Buchungen werden zerlegt statt als Zeilen ohne Gegenkonto
  exportiert.
- Der Kontenrahmen SKR03/SKR04 wird erkannt (`detect_company_chart`), im Kopffeld 27
  „Sachkontenrahmen“ übergeben und auf der Berichte-Seite am Download genannt; die
  Kopfzeile hat alle 31 Felder des DATEV-Musters.
- Neu: `data/kontenrahmen/datev_kontenfunktionen.csv` (Automatik- und Zusatzfunktionen
  aus den DATEV-Kontenrahmen 2026) und `tools/datev_kontenfunktionen.py` zum Erneuern.
- MCP: API-Antworten werden im Zeichensatz des Content-Type gelesen; `export_datev_csv`
  liefert Umlaute damit korrekt (vorher Ersatzzeichen, weil Windows-1252 als UTF-8 galt).

### UStVA: Steuer auch ohne Steuercodes

- UStVA und DATEV-Export erkennen Steuerzeilen über dieselbe Funktion
  (`company_tax_accounts`): die Steuerkonten der Steuercodes und – auch wenn eine
  Gesellschaft gar keine Steuercodes hat – die Standard-Steuerkonten 1571/1576/1771/1776
  (SKR03) bzw. 1401/1406/3801/3806 (SKR04), sofern Kontoart und Bezeichnung passen.
  Bisher zählte die UStVA ohne Steuercodes alle Erlöse als steuerfrei (Kz 48) und
  keine Vorsteuer (betraf z. B. Buchungen auf alten SKR04-Steuerkonten in einer
  SKR03-Buchhaltung).
- Umbuchungen zwischen Steuerkonten (Bereinigung 3806 → 1776, 1406 → 1576) sind für die
  UStVA neutral; der Hinweis der Kontenrahmen-Prüfung zu Steuerkonten mit Steuercode
  nennt deshalb nur noch das Deaktivieren als Problem.

### KI-Zugang (LLM-API-Key) je Benutzer

- Der Administrator hinterlegt je Benutzer einen KI-Zugang: Standard OpenAI
  (`https://api.openai.com/v1/responses`, nur API-Key und optional Modell) oder ein
  anderer OpenAI-`/responses`-kompatibler Endpunkt (Azure, OpenRouter, Ollama …;
  Key optional). Der Key liegt Fernet-verschlüsselt (Schlüssel aus `SECRET_KEY`
  abgeleitet) in `user.llm_api_key_encrypted`, ausgegeben werden nur die letzten
  vier Zeichen; Audit-Ereignisse `llm_settings_updated`/`llm_settings_cleared`.
- KI-Chat, Beleg-OCR, KI-Kontrolle, Belegabgleich und Dokument-Update verwenden den
  Zugang des handelnden Benutzers und senden den Key als Bearer-Header; ohne Zugang
  gelten die Instanz-Endpoints (`*_LLM_ENDPOINT_URL`) wie bisher. PDF-Scans gehen
  als `input_file` an den OCR-Endpoint (von OpenAI akzeptiertes Format).
- UI: Abschnitt **KI-Zugang (LLM) je Benutzer** in der Verwaltung; API:
  `POST /api/v1/users/<id>/llm`, `POST /api/v1/users/<id>/llm/delete`, Feld `llm`
  in der Benutzerausgabe; MCP: `set_user_llm_settings`, `clear_user_llm_settings`
  (im KI-Chat gesperrt). Migration `20261009_0043`.
- **Entfernt:** `CHAT_LLM_API_KEY` wird nicht mehr ausgewertet (Warnung beim Start).
- Neue Abhängigkeit `cryptography`.

### Geschäftspartner (Debitoren/Kreditoren), PR 1

- Neuer Kunden-/Lieferantenstamm `business_partner`: Debitoren- (10000–69999) und
  Kreditorennummern (70000–99999) mit automatischer Vergabe, Art, Anschrift,
  USt-IdNr, Steuernummer, Kontakt, Zahlungsziel, Notizen, Aktivstatus. Nummern und
  Rollen sind nach der ersten Verwendung fest; kein Löschen, nur Deaktivieren;
  Dublettenhinweise (USt-IdNr, IBAN, Name). Vorher-/Nachher-Historie, Bankdaten
  nur über eigenen Endpunkt (`bank_details_changed`), im KI-Chat gesperrt.
- Nebenbuch statt Personenkonten (ADR-002): Kontenkennzeichen `subledger`
  (`debtor`/`creditor`), `partner_id` an Buchungszeilen nur auf passenden
  Sammelkonten. Festschreibungs-Hash Version 3 inkl. Partner; Version-2-Siegel
  bleiben unverändert gültig. Storno, Buchungsvorlagen, Eröffnungsbilanz
  (vierte Spalte Partnernummer) und Saldovortrag (je Partner) übernehmen den
  Partner; Journal-API und Journal-CSV zeigen Partnernummer und -name.
- UI: Seite **Partner** (Liste, Anlage, Detail mit Bankdaten und Historie),
  Partnerauswahl je Buchungszeile, Sammelkonto-Spalte unter **Konten**.
  API: `/api/v1/partners` (+ `/<id>`, `/<id>/bank-details`, `/<id>/history`);
  MCP: `list_partners`, `get_partner`, `create_partner`, `update_partner`,
  `set_partner_bank_details`, `get_partner_history`. Prüferexport:
  `business_partners.json`, `business_partner_history.json`.
- Kontoarten werden auch bei der Kontoanlage (UI/API/MCP) geprüft; `receivable`/
  `payable` sind keine gültigen Kontoarten mehr (Bilanz und Saldovortrag werten
  sie nicht aus) und erscheinen in der Kontenrahmen-Prüfung als unbekannte
  Kontoart. Ungültige Altwerte (z. B. 4240 „Strom“ aus dem alten SKR04-Import)
  lassen sich per UI/API/MCP reparieren. Der Kontenrahmen-Import kennzeichnet
  Forderungen/Verbindlichkeiten aLuL an der exakten Bezeichnung als Sammelkonto
  oder nimmt eine optionale Spalte `subledger`.
- Sammelkonten erscheinen nicht mehr in der Bankkontenauswahl.
- Migration `20261008_0042` (keine Datenänderung an Bestandskonten:
  Sammelkonten bitte unter **Konten** kennzeichnen).

### Kontenrahmen SKR04

- `data/kontenrahmen/skr04.csv` neu aufgestellt nach DATEV-Kontenrahmen SKR04 2026
  (Art.-Nr. 11175), Umfang wie `skr03.csv` (30 Konten — „Aufwendungen für bezogene
  Leistungen“ ist im SKR04 die GuV-Position von 5900 Fremdleistungen): u. a. Kasse 1600,
  Bank 1800, Geldtransit 1460, Forderungen aLuL 1200, Verbindlichkeiten aLuL 3300,
  Erlöse 19 %/7 % 4400/4300, Aufwendungen 5100–6930. Die alte Datei mischte SKR03-Nummern
  (1000 Kasse, 1200 Bank, 1600 Verbindlichkeiten, 8000/8300 Erlöse …) mit
  SKR04-Steuerkonten; „Gas, Strom, Wasser“ landete wegen eines unmaskierten Kommas als
  Konto „Gas“ mit Kontoart „Strom“.
- Funktionskonten kontenrahmensicher (`app/services/standard_accounts.py`): Geldtransit
  nur über die Bezeichnung (SKR04 1360 = Darlehen, SKR03 1460 = Zweifelhafte
  Forderungen), Gewinnvortrag 0860/2970 per Nummer nur mit Kontoart `equity` (SKR04 0860
  = Beteiligungen an Personengesellschaften), Saldenvortrag bevorzugt 9000. Bank-,
  Beleg-OCR-, Belegabgleich- und E-Rechnungsmaske wählen Bank- bzw. Kreditorenkonto
  passend zum Kontenrahmen vor statt fest 1200/1600 (im SKR04 Forderungen bzw. Kasse).
- Kontenrahmen-Import lehnt Zeilen mit mehr Spalten als die Kopfzeile und unbekannte
  Kontoarten ab; die Kontoart wird kleingeschrieben übernommen.
- Neu: Kontenrahmen-Prüfung — UI-Hinweis auf **Konten**, `GET /api/v1/account-chart/check`,
  MCP-Tool `check_account_chart`. Meldet Altkonten aus dem fehlerhaften SKR04-Import mit
  richtiger SKR04-Nummer, Buchungsanzahl und Saldo sowie Konten mit unbekannter Kontoart.
  Bestehende Gesellschaften werden nicht automatisch umgebaut (Kontonummern bleiben
  unveränderlich, GoBD); Vorgehen im README unter „Kontenrahmenimport“.

### OAuth für MCP-Connectoren

- OAuth-2.1-Server nach MCP-Autorisierungsspezifikation, damit ChatGPT- und
  claude.ai-Connectoren sich anmelden können (beide senden keine festen Bearer-Tokens):
  Discovery (RFC 9728/8414), Dynamic Client Registration (RFC 7591),
  Authorization-Code-Flow mit PKCE S256, Login und Zustimmungsseite, Refresh-Token-Rotation,
  Widerruf (RFC 7009). Access-Tokens wirken wie Benutzer-Tokens (Rolle, Mandant, Audit).
- MCP-HTTP antwortet bei 401 mit `WWW-Authenticate: Bearer resource_metadata=…`.
- Verbundene Apps: UI-Seite **Apps**, API `GET /api/v1/oauth/grants`,
  `POST /api/v1/oauth/grants/<id>/revoke`, MCP-Tools `list_oauth_grants`,
  `revoke_oauth_grant`. Migration `20261008_0041`.

### MCP

- MCP-HTTP-Endpunkt akzeptiert Benutzer-API-Tokens (`MCP_HTTP_ALLOW_USER_TOKENS`,
  Default an): Prüfung über `GET /api/v1/users/me`, Weitergabe pro Request an die
  REST-API — Tools laufen mit Rolle, Mandant und Audit-Akteur des Benutzers.
- Neuer Endpunkt `GET /api/v1/users/me` und MCP-Tool `get_current_user`.

### Betrieb

- Overlay `docker-compose.nginx.yml`: nginx-Reverse-Proxy mit Let's-Encrypt-Zertifikat
  (certbot, HTTP-01) für eine öffentlich erreichbare Instanz — Web-UI, REST-API und
  `/mcp` unter einer Domain (`PUBLIC_DOMAIN`), Login-Drossel, Platzhalter-Zertifikat
  bis zur ersten Ausstellung, automatische Erneuerung und nginx-Reload.
- `redeploy.sh`/`backup.sh` lesen `COMPOSE_FILE` auch aus der `.env` und akzeptieren
  mehrere Compose-Dateien (`:`-getrennt); Host-Port des MCP-Servers per `MCP_PORT`.

### Behoben

- Kontenrahmen-Prüfung erkennt SKR03-Buchhaltung nach dem fehlerhaften SKR04-Import.
  Bisher galt jede Gesellschaft mit SKR04-Steuerkonten als SKR04-Gesellschaft; bucht
  sie faktisch SKR03, meldete die Prüfung ihre richtigen SKR03-Konten als Altkonten
  und empfahl die Umbuchung in die falsche Richtung. Jetzt bestimmt sie den
  vorherrschenden Kontenrahmen (`dominant_chart`, `chart_evidence`) an den Konten
  außerhalb des alten Imports, deren Nummernbereich nur in einem Kontenrahmen zur
  Kontoart passt, samt Buchungszeilen. Bei SKR03 meldet sie die SKR04-Fremdkonten
  0420, 1401/1406, 2100/2180, 2970 und 3801/3806 mit SKR03-Gegenkonto (0200,
  1571/1576, 1800/1890, 0860, 1771/1776 laut DATEV-Kontenrahmen SKR03 2026),
  Bedeutung der Nummer im SKR03, Buchungsanzahl, Saldo, Belegung der SKR03-Nummer
  und verweisenden Steuercodes (`foreign_skr04_accounts`; Steuerkonten mit
  Steuercode nicht umbuchen, weil die UStVA Steuerzeilen darüber erkennt). Als
  Hinweis ohne Einfluss auf `ok` kommen die Nummern der alten Datei hinzu, die im
  SKR03 ein anderes Konto bezeichnen oder kein Einzelkonto sind: 0400, 3400, 4800,
  6200 und 8000 mit Standardkonto 0010, 3100, 4260, 4830 bzw. 8400
  (`nonstandard_skr03_accounts`). Gilt für die Seite **Konten**,
  `GET /api/v1/account-chart/check` und das MCP-Tool `check_account_chart`; die
  Prüfung bleibt lesend, Vorgehen im README unter „Kontenrahmen-Prüfung und
  Altbestände“.
- Kostenstellen- und Profitcenter-Bericht zählen Erlöskonten vom Typ `income` als
  Erlös (Filter, Vorzeichen, `total_revenue`) — wie die GuV über
  `REVENUE_ACCOUNT_TYPES`. Bisher fehlten bei SKR03/SKR04-Kontenrahmen und bei in
  der UI angelegten Ertragskonten sämtliche Erlöse; betroffen waren UI,
  `GET /api/v1/controlling-report`, `/api/v1/exports/controlling.csv` und die
  MCP-Tools `get_controlling_report`/`export_controlling_csv`.

## [0.1.0] – 2026-09-20

Erstes versioniertes Release. Fasst den bis dahin auf `main` erreichten Stand
zusammen (siehe `docs/review/projektreview-2026-09-19.md` für die Bewertung).

### Betrieb (Sprint 3, PR #149)

- `redeploy.sh` arbeitet ausschließlich mit `docker-compose.production.yml`,
  zieht vor `compose down` ein Backup, gibt Commit/Tag aus, führt
  `alembic upgrade head` als eigenen Schritt aus und wartet auf den Health-Check.
- `backup.sh`: `pg_dump` (Custom-Format), Archiv der Belegablage, Metadaten
  (Commit, Alembic-Revision, Image), Prüfsummen, Aufbewahrung; Restore-Runbook in
  `docs/compliance/produktionsbetrieb.md`; systemd-Timer für Backup und
  `verify-integrity` unter `deploy/systemd/`.
- Dockerfile: Basis-Image per Digest gepinnt, unprivilegierter Benutzer `app`,
  `HEALTHCHECK` gegen `/api/v1/health`, gunicorn als `CMD`; `.dockerignore`
  hält `instance/`, `.git`, `.venv`, Tests und Agenten-Verzeichnisse aus dem Image.
- gunicorn mit `--timeout 180 --graceful-timeout 30`, Access-Log mit Dauer und
  Request-ID; `healthcheck` für den `app`-Service im Compose.
- `DB_AUTO_MIGRATE`: Auto-Migration beim Start abschaltbar (Produktion: aus,
  fail-fast bei Schema-Rückstand); `flask`-CLI-Aufrufe migrieren eine bestehende
  Datenbank nicht mehr nebenbei.
- Logging über `dictConfig` (`LOG_LEVEL`, `LOG_FORMAT=text|json`), Request-ID
  je Request (`X-Request-ID` ein- und ausgehend), Alembic-`fileConfig` ohne
  `disable_existing_loggers`.
- Health-Endpoint mit `SELECT 1`, Alembic-Revision gegen Head, Version und Commit
  (503 bei Störung).
- Engine mit `pool_pre_ping`; SQLite mit `PRAGMA foreign_keys=ON` und WAL.
- Versionsnummer in `pyproject.toml`, dieses Changelog, `.env.example` und
  ENV-Referenz im README; ELSTER-/Lohn-Runner-Optionen aus der Umgebung lesbar.
- `worker`-Platzhalter (und ungenutztes `redis`) aus `docker-compose.yml`
  entfernt; Caddyfile mit Sicherheits-Headern, dokumentierter IP-Allowlist und
  optionalem Rate-Limit (`deploy/caddy/Dockerfile`).

### Fachliche Korrektheit (Sprint 1, PR #147)

- Abschlussbuchungen (Periode 13) in SuSa/GuV/Bilanz/UStVA/KSt standardmäßig
  ausgeklammert (Schalter `include_closing_entries`), Jahresabschluss atomar mit
  Saldovortrag ins Folgejahr; Netto-aus-Brutto mit Rundungscent auf der
  Steuerzeile; Buchung und Fachoperation in einer Transaktion (AfA, Lohn,
  Belegabgleich, Storno); `TaxCode.kind` (input/output) mit Validierung;
  Buchungsnummern je Geschäftsjahr mit Sequenztabelle und Retry; Storno-Hooks
  für Bankumsatz, AfA, Lohnlauf und offene Posten; Cent-Quantisierung,
  Stornodatum ≥ Originaldatum, Prüfung überlappender Geschäftsjahre.

### Sicherheit (Sprint 2, PR #148)

- KI-Chat: nur lesende Tools sofort, schreibende mit Bestätigungsschritt,
  sensible Tools gesperrt, Anhänge und Tool-Ergebnisse als „untrusted data“,
  Secret-Redaktion vor Persistierung; `/api/v1/mcp/call` in-process mit
  Aufruferkontext; `ProxyFix` (`TRUSTED_PROXY_COUNT`), Login-Rate-Limit in der
  Tabelle `login_attempt`, Secure-Cookie/HSTS/Session-Laufzeit in Produktion,
  Open-Redirect-Schutz, Admin-Check bei Mandantenanlage, MCP-HTTP-Body-Limit,
  Audit-Events für Login/Token/Benutzer/Passwort, Passwort-ändern-Flow
  (UI/API/MCP/CLI).

### Funktionsumfang zum Release

- Kernbuchhaltung: Mandanten, Gesellschaften, Kontenrahmen (SKR03/SKR04-Import),
  Steuercodes, mehrzeilige Journalbuchungen mit Validierung, Storno,
  Festschreibung mit DB-seitigem Schutz, Buchungsvorlagen, Eröffnungsbilanz.
- Belege: Upload mit Hash und Versionierung, OCR-/LLM-gestützte
  Buchungsvorschläge, Belegabgleich, E-Rechnung (XRechnung/ZUGFeRD) Import/Export.
- Bank und OPOS: Kontoauszugs-Import (CSV, CAMT.053, MT940), FinTS-Direktabruf
  mit TAN-Flow, Bankregeln, Zahlungsabgleich, offene Posten, Mahnwesen,
  SEPA-Zahllauf (pain.001).
- Anlagenbuchhaltung (AfA, GWG, Sammelposten, Abgang) und Lohn-MVP.
- Auswertungen: Summen-/Saldenliste, GuV, Bilanz, Kostenstellen/Profitcenter,
  UStVA-/USt-Jahres-Snapshots mit ELSTER-Preflight, KSt/GewSt-Arbeitssnapshots,
  DATEV-Buchungsstapel, Prüferexport mit Manifest und Hashes.
- Schnittstellen: REST-API mit Token-Auth, MCP-Server (stdio und Streamable
  HTTP), KI-Chat mit Tool-Zugriff; Rollen, Tenant-Scoping, append-only Audit-Log
  mit Hashkette, Integritätsprüfung per UI/API/MCP/CLI.

[Unreleased]: https://github.com/unikinCK/OpenBuchhaltung/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/unikinCK/OpenBuchhaltung/releases/tag/v0.1.0
