# Projekt-Review – Stand 2026-09-19

## 0. Zusammenfassung

OpenBuchhaltung ist funktional weit gekommen: 125 API-Routen, 92 UI-Routen,
122 MCP-Tools, 441 grüne Tests, ein einziger zentraler Buchungspfad mit
DB-seitiger Unveränderbarkeit und Audit-Hashkette. Die Architektur trägt.
Die Schwächen liegen nicht in fehlender Breite, sondern in fachlicher Tiefe,
Betriebsreife und ein paar Sicherheitsentscheidungen rund um den KI-Chat.

Die zehn wichtigsten Aussagen:

1. **Jahresabschluss verfälscht Auswertungen.** Der Ergebnisvortrag bucht gegen
   die Erfolgskonten in Periode 13, aber GuV, KSt-Ermittlung und UStVA
   klammern Abschlussbuchungen nicht aus. Nach `close_fiscal_year` ist die
   GuV des Jahres 0, das zu versteuernde Einkommen 0 und Kz 48 negativ.
2. **Netto-aus-Brutto scheitert bei ~16 % aller Bruttobeträge** (19 %) mit
   einem Fehler statt einer Rundungszeile.
3. **Storno wirkt nur im Journal.** Bankumsatz, AfA-Satz, Lohnlauf, OPOS bleiben
   „gebucht“; Anlagenspiegel und Hauptbuch laufen auseinander.
4. **DATEV-Export ist so nicht importierbar** (alle Jahre in einem Stapel,
   Splitbuchungen ohne Gegenkonto, keine BU-Schlüssel).
5. **Der KI-Chat darf alle schreibenden Tools ohne Rückfrage aufrufen**, auch
   Benutzer anlegen, Token rotieren, ELSTER einreichen, SEPA-Läufe erzeugen.
   Anhänge und Bank-Verwendungszwecke sind ungefilterte Prompt-Injection-
   Vektoren; FinTS-PINs landen im Klartext im Chat-Verlauf.
6. **`redeploy.sh` rollt die Dev-Compose-Datei aus** (Flask-Dev-Server,
   Bind-Mount, `APP_ENV=development`), nicht die Produktions-Compose.
7. **Kein Backup/Restore, kein `.dockerignore`** (Belege und DB landen im
   Image-Layer), keine Versionierung, gunicorn-Timeout 30 s bei bis zu
   120 s langen Chat-/OCR-Requests.
8. **Vier Services committen mitten in der Fachoperation** (AfA, Lohn,
   Belegabgleich, Storno) und hinterlassen bei Fehlern Waisenbuchungen.
9. **CI testet nur SQLite**, Postgres-spezifische Pfade (Immutability-
   Trigger-Zweig, `FOR UPDATE`, NUL-Bytes) laufen nie; keine Coverage,
   keine Typprüfung.
10. **Produktseitig fehlen Kunden-/Lieferantenstamm, Ausgangsrechnungen,
    Firmenstammdaten in der DB, §13b/ZM/innergemeinschaftliche Fälle,
    Eingangsrechnung→OPOS, BWA und ein deutsches Zahlenformat in der UI.**

Empfohlene Reihenfolge: Sprint 1 Fachlogik (Punkte 1–4, 8), Sprint 2
Sicherheit (5), Sprint 3 Betrieb (6, 7), Sprint 4 Qualität (9), danach die
Produktlücken (10). Details in Abschnitt 6.

## 1. Ziel und Bewertungsbasis

Dieses Review beantwortet drei Fragen: Wo steht der Code qualitativ, was
sollten wir besser machen, und welche Features fehlen für die Zielgruppe
(kleine Kapitalgesellschaft, Buchhaltung selbst, Abschluss beim Steuerberater).

Grundlage: vollständige Lektüre der Schichten `app/api`, `app/web`,
`app/services`, `domain/models.py`, Templates, CI/Deployment-Konfiguration,
`docs/umsetzungsplan.md`, `docs/compliance/*`, Testsuite und GitHub-Historie
(Stand Commit `1eb48f3`, Merge von PR #145). Vorheriges Review:
`projektreview-2026-04-05.md`; letztes Review-Follow-up: PR #137 (2026-08-29).
Sieben getrennte Prüfpfade (Fachlogik, Sicherheit, Architektur, Tests, UI,
Betrieb, Feature-Abgleich); jeder Befund ist mit Datei und Zeile belegt,
Unbestätigtes ist markiert.

## 2. Kennzahlen (Baseline)

| Kennzahl | Wert |
|---|---|
| App-Code (`app/` + `domain/`) | ca. 31.800 Zeilen Python in 102 Dateien |
| davon `app/services` / `app/api` / `app/web` | 17.100 / 6.850 / 5.475 Zeilen |
| Routen API / Web / MCP-Tools | 125 / 92 / 122 |
| größte Datei | `app/services/mcp_server.py` (3.405 Zeilen, davon 3.100 Tool-Literal) |
| ORM-Modelle | 30 in `domain/models.py` (1.362 Zeilen); 40× `Numeric`, 0× `Float` |
| Funktionen > 100 Zeilen | 28 von 786; größte `build_audit_export_package` (364) |
| Templates / Static | 27 Templates (7.700 Zeilen), 1.300 Zeilen CSS/JS |
| Tests | 441 in 41 Dateien (18.700 Zeilen), 3× `e2e`, 1× `performance` |
| Testlaufzeit lokal | 65 s (SQLite), ruff sauber, keine Warnungen |
| Alembic-Migrationen | 36, alle mit `downgrade()`, Rundlauf up/down funktioniert |
| Typisierung / Docstrings | 66 % Return-Annotationen, 20 % Docstrings |
| Logging | 5 Dateien nutzen `logging`, keine Konfiguration |
| Commits / PRs | 259 Commits, 145 PRs, 59 Commits seit 2026-08-01 |
| Offene Issues / PRs / Tags | 0 / 0 / 0 |
| Python | Ziel 3.12 (pyproject, CI, Docker); lokales venv läuft auf 3.10.12 |

## 3. Befunde nach Bereich

Schweregrade: **K** kritisch (falsche Zahlen/Compliance), **H** hoch,
**M** mittel, **N** niedrig. Aufwand: S < 1 Tag, M 1–3 Tage, L > 3 Tage.

### 3.1 Fachlogik (HGB / GoBD / Steuern)

| Nr. | Grad | Befund | Beleg | Fix | Aufw. |
|---|---|---|---|---|---|
| F1 | K | **Abschlussbuchungen nicht ausgeklammert.** `close_fiscal_year` stellt jedes Erfolgskonto per Gegenzeile mit `entry_date = WJ-Ende` in der Abschlussperiode glatt. Reports filtern nur nach `entry_date`, kennen kein `is_closing`; KSt nutzt dieselbe GuV; UStVA wertet die Vortragszeile auf 8400 (ohne Steuerzeile) als steuerfrei. Beispiel: Erlöse 100.000, Aufwand 60.000 → nach Abschluss GuV = 0, zvE = 0, Kz 48 Dezember = −100.000. `tests/test_periods.py:355` verankert das als Sollverhalten. | `periods.py:273-331`, `reports.py:17-23,111-134`, `income_taxes.py:186-192`, `vat_returns.py:261-278` | Reports/UStVA/KSt standardmäßig ohne Buchungen aus `is_closing`-Perioden (Schalter „inkl. Abschluss“); Test umdrehen | M |
| F2 | H | **Netto-aus-Brutto ohne Rundungszeile.** Sucht ein Netto, dessen gerundete Steuer exakt aufgeht; bei 19 % gibt es für ~16 % aller Cent-Beträge keins (1,22 € → Fehler). | `bank_import.py:865-882` | Steuer = Brutto − Netto als explizite Zeile, Rundungscent auf der Steuerzeile | S |
| F3 | H | **DATEV-Export nicht importierbar.** Alle Jahre ohne WJ-Filter; WJ-Beginn = 1.1. des ältesten Belegs; Belegdatum TTMM → Mehrjahresstapel in einem WJ. Splitbuchungen ohne Gegenkonto (Pflichtfeld); keine BU-Schlüssel, Steuer als eigene Zeile → DATEV rechnet auf Automatikkonten erneut. | `datev_export.py:159-167,190,72,227-243` | Export je WJ; Splits gegen Gegenkonto; Steuerzeilen in BU-Schlüssel überführen; Golden-File-Test | M |
| F4 | H | **Storno ohne Nebenbücher.** Storno ändert nur das Journal; Bankumsatz bleibt „booked“, `DepreciationEntry` bleibt (Buchwert ≠ Hauptbuch), Lohnlauf bleibt „posted“, OPOS bleibt ausgeglichen. | `journal_entries.py:732-822`; kein Storno-Code in `bank_import.py`, `open_items.py`, `fixed_assets.py`, `payroll.py` | Storno-Hook je Quelle: Bank → „open“, AfA-Satz stornieren, OPOS reaktivieren, Lohnlauf zurücksetzen | M |
| F5 | H | **Steuercode ohne Richtung.** Kein input/output-Typ; VSt19 auf Erlöszeile bucht Haben 1576 → Kz 66 sinkt statt Kz 81 steigt. | `models.py:292-316`, `journal_entries.py:342-407`, `incoming_invoice.py:84-102` | `TaxCode.kind` + Validierung gegen Kontoart und Soll-/Haben-Seite | S |
| F6 | M | **Buchungsnummern**: Jahrespräfix + gesellschaftsweiter `COUNT+1` → erste 2026er-Buchung nach 120 Vorjahresbuchungen heißt „2026-0121“, Nachbuchung 2025 danach „2025-0122“. Parallelbuchung → gleiche Nummer → 409 ohne Retry. | `journal_entries.py:622-629`, `api/journal.py:257-261` | Zähler je WJ in Sequenztabelle mit `FOR UPDATE`; Retry im Service | S |
| F7 | M | **Jahresabschluss unvollständig**: Vortrag committet vor dem Sperren (nicht atomar); keine Prüfung auf Vorjahresabschluss, offene Bankumsätze, ungebuchte AfA, nicht festgeschriebene Buchungen; kein Saldovortrag der Bestandskonten → SuSa je WJ ohne EB-Werte. | `periods.py:320,385-414`, `reports.py:33-71` | Abschluss-Checkliste + Saldovortrag ins Folgejahr; ein Commit | M |
| F8 | M | **OPOS entkoppelt vom Hauptbuch**: Ausgleich ohne Buchung möglich; kein Skonto; Zahllauf markiert Posten nicht → Doppelzahlung beim zweiten Lauf. | `open_items.py:123-184`, `sepa_export.py:120-249` | Ausgleich immer mit Buchung; Skonto-Differenz → Skontokonto + USt-Korrektur; Zahllauf setzt „in Zahlung“ | M |
| F9 | M | **AfA**: Plan kalenderjährlich, Buchung am 31.12. → abweichendes WJ falsch. Abgang bucht RBW ohne zeitanteilige AfA gegen das planmäßige AfA-Konto statt 2310/6895. | `depreciation.py:154-183`, `fixed_assets.py:620,685-697` | WJ-Grenzen aus `FiscalYear`; Abgangsbuchung mit anteiliger AfA und Abgangskonto | M |
| F10 | M | **UStVA-Fallback „Ertrag ohne USt-Zeile = Kz 48“** erfasst nicht steuerbare Erträge (Versicherung, Anlagenverkauf) und ig. Lieferungen/Ausfuhren als steuerfrei; Kz 83 aus gebuchter statt errechneter USt. | `vat_returns.py:270-278,311-314` | Kennziffer aus Steuercode-Typ ableiten; Warnung statt stiller Zuordnung | M |
| F11 | M | **Lohn**: Journal-Commit vor Statuswechsel → Doppelbuchung bei Retry; Aufwand auf Zahlungsdatum statt Lohnmonat. | `payroll.py:325-345,329` | `commit=False`; Buchungsdatum = Monatsende der Abrechnung | S |
| F12 | M | **Cent-Quantisierung fehlt**: `parse_decimal` und Validator akzeptieren 0,015 + 0,015 gegen 0,03. SQLite bindet `Numeric` vermutlich als Float (unbestätigt). | `journal_entries.py:410-414`, `journal_entry_validation.py:58-61` | `quantize(0.01)` + Fehler bei > 2 Dezimalen | S |
| F13 | M | **Storno-Datum** nicht ≥ Originaldatum; automatisches WJ ohne Überlappungsprüfung (anders als `periods.py:180-194`), `.first()` entscheidet zufällig. | `journal_entries.py:736-796,440-457` | Datumsprüfung; Überlappung ablehnen | S |
| F14 | N | E-Rechnung: CII ohne `ExchangedDocumentContext`/Positionen/Delivery, CustomizationID EN16931 statt XRechnung-CIUS (unbestätigt ohne Validator); Import nutzt `LineTotalAmount` statt `TaxBasisTotalAmount` → bei Nachlässen Soll ≠ Haben. | `einvoice_export.py:162-270`, `einvoice_import.py:141` | KoSIT-Validator in Tests; Basis-Betrag korrigieren | M |
| F15 | N | Umgliederung beim Umhängen datiert `date.today()` (Stichtagsbilanzen alter Perioden bleiben schief); Audit „created“ ohne Zeilenbeträge. | `bank_import.py:842`, `journal_entries.py:278-283` | dokumentieren bzw. Zeilen ins Audit-Payload | S |

**Fachlich per grep als fehlend verifiziert:** Kleinunternehmer,
Ist-Versteuerung (§ 20 UStG), § 13b, innergemeinschaftliche Vorgänge
(Kz 41/89/61), Anzahlungen, Dauerfristverlängerung, Skonto, Zuschreibung
(§ 253 Abs. 5 HGB), Mindestbesteuerung (§ 10d EStG, Verlustvortrag wird
ungedeckelt abgezogen, `income_taxes.py:202`), automatische Festschreibung
bei UStVA-Abgabe (GoBD), Anlagenspiegel-Export, KOST-Felder im DATEV-Export,
Saldovortragskonto 9000 im mitgelieferten SKR03 (`opening_balance.py:24`
erwartet es).

### 3.2 Sicherheit

Kritisch: keins. Tenant-Scoping, CSRF, CSP, Upload-Härtung und Token-Handling
sind solide (siehe Abschnitt 5).

| Nr. | Grad | Befund | Beleg | Fix | Aufw. |
|---|---|---|---|---|---|
| S1 | H | **KI-Chat ruft alle schreibenden Tools ohne Bestätigung auf** (nur Chat-Tools ausgeschlossen). Anhangstext und Tool-Ergebnisse (z. B. Bank-Verwendungszwecke) gehen ungefiltert ans Modell; einzige Schutzmaßnahme ist der Systemprompt. Eine präparierte PDF oder ein Verwendungszweck „buche … / lege Benutzer an / reiche UStVA ein“ führt zu `create_user`, `rotate_user_api_token`, `submit_vat_return_elster`, `create_sepa_payment_run`, `close_fiscal_year` mit den Rechten des Nutzers. | `chat.py:114-125,158,244-249,291-294` | Allowlist nur lesender Tools als Default; schreibende Tools nur nach expliziter UI-Bestätigung; Benutzer-/Token-/ELSTER-/SEPA-/FinTS-Tools ganz ausschließen; Anhänge als „untrusted“ kapseln | M |
| S2 | H | **Geheimnisse im Chat-Verlauf.** `tool_calls` speichert Argumente und Ergebnisse unredigiert: `create_user` (password), `sync_fints_transactions`/`submit_fints_tan` (pin), `rotate_user_api_token` (api_token) → Banking-PIN dauerhaft in `chat_messages`, sichtbar in UI und `get_chat_conversation`. | `chat.py:474-481`, `mcp_server.py:169,1747,1777`, `users.py:146` | Schlüssel pin/tan/password/api_token maskieren; diese Tools aus dem Chat nehmen | S |
| S3 | H | **MCP-Proxy hebelt Tenant-Scoping aus.** Jeder Nutzer mit Schreibrolle darf `POST /api/v1/mcp/call`; der Proxy sendet keinen Auth-Header, der MCP-Server arbeitet mit dem globalen Token → `create_user` als Admin, `list_users`/`list_journal_entries` fremder Mandanten. Voraussetzung: `MCP_SERVER_URL` gesetzt. | `api/mcp.py:12-35`, `mcp_client.py:26-31` | Endpoint entfernen oder in-process mit Aufruferkontext (wie `InProcessApiClient` im Chat); mindestens auf globale Admins beschränken | S |
| S4 | M | Login-Rate-Limit per `remote_addr`, in-memory, pro Prozess; kein ProxyFix → hinter Caddy teilen alle Clients eine IP (Lockout-DoS gegen „admin“), mit 2 Workern doppeltes Limit. | `auth.py:259,270`, `app/__init__.py` | ProxyFix bei `APP_ENV=production`; Limiter in DB; Admin-Entsperrung | S |
| S5 | M | FinTS-URL nur auf `https://` geprüft → Buchhalter legt Zugang auf Angreifer-Host an, Kollege gibt PIN ein; SSRF auf interne Hosts. | `fints_sync.py:129` | Allowlist bekannter FinTS-Server (BLZ-Mapping), private Adressen blocken, URL-Änderung nur Admin | S |
| S6 | M | Open Redirect via Backslash: `next=/\evil.com` passiert die Prüfung, Werkzeug gibt es unverändert aus, Browser normalisieren zu `//evil.com`. | `auth.py:338-340` | `urlsplit` auf leere scheme/netloc prüfen, `\` ablehnen | S |
| S7 | M | Cookie-/Session-Härtung: `SESSION_COOKIE_SECURE` Default aus, kein HSTS, keine Session-Laufzeit/Idle-Timeout. | `app/__init__.py:31,128-141` | Secure-Flag in Produktion erzwingen, HSTS, `PERMANENT_SESSION_LIFETIME` | S |
| S8 | M | Rollenprüfung UI ≠ API bei Mandantenanlage: UI prüft nur „global“, API verlangt Admin. | `web/admin.py:71-75` vs. `api/helpers.py:58-62` | `_is_admin()` ergänzen | S |
| S9 | M | MCP-HTTP: `Content-Length` unbegrenzt gelesen (Speicher-DoS), statisches Token mit globalen Rechten, kein Rate-Limit. | `mcp_http.py:128-135,178-179` | Body-Limit; MCP-Token an Benutzer-Token koppeln; IP-Allowlist im Caddyfile aktivieren | S |
| S10 | N | `secrets.compare_digest` wirft bei Nicht-ASCII `TypeError` → 500; Upstream-Fehlertexte (LLM, OCR, FinTS inkl. URL) gehen an Clients; API-Token im Flash (Session-Cookie); `--password` als CLI-Option; `seed-demo` legt `admin/admin123` auch in Produktion an; Legacy-Token-Scan über alle Hashes bei jedem ungültigen Token; Username-Enumeration per Timing; keine Passwortrichtlinie; CSRF-Token bei Login nicht rotiert. | `auth.py:127,195,225-238,321`, `chat_llm.py:70`, `receipt_ocr.py:305`, `fints_sync.py:252`, `web/admin.py:162`, `cli.py:40-46,167,216` | jeweils Einzeiler bis wenige Zeilen | S |

**Fehlende Security-Features:** 2FA/TOTP oder Passkeys; Passwort ändern/
zurücksetzen (es gibt keinerlei Flow); serverseitige Sessions mit Widerruf und
Invalidierung bei Rollen-/Passwortänderung; Token-Scopes und Ablaufdatum
(ein Token pro Nutzer mit vollen Rechten); Audit-Einträge für Logins,
Fehlversuche, Token-Rotation, Benutzeranlage (`users.py`, `admin.py`,
`auth.py` rufen `log_audit_event` nicht auf); Rate-Limiting für Chat/OCR
(LLM-Kosten). Fehlende Tests: Open Redirect, Cookie-Flags, MCP-Proxy-Rechte,
Chat-Schreibtools/Prompt-Injection, FinTS-URL-Validierung.

### 3.3 Architektur und Code-Qualität

| Nr. | Grad | Befund | Beleg | Fix | Aufw. |
|---|---|---|---|---|---|
| A1 | H | **Halbfertige Commits in Multi-Step-Services.** `create_journal_entry` committet per Default; vier Ketten schreiben danach weiter mit zweitem Commit: AfA (Buchung ohne `DepreciationEntry` → Retry bucht doppelt), Lohn (Buchung, Lauf bleibt „draft“), Belegabgleich (Buchung ohne Verknüpfung), Storno. Korrekt gelöst in `bank_import.py:951` und `journal_templates.py:227` (`commit=False`). | `journal_entries.py:281,782`, `fixed_assets.py:494-535`, `payroll.py:325-345`, `receipt_matching.py:577-615` | `commit=False` an den vier Stellen; mittelfristig Unit-of-Work: Services nur `flush()`, Commit in der Route | S |
| A2 | M | **MCP-Tool-Liste ist ein 3.100-Zeilen-Handliteral** (122 `ToolSpec`, `:116-3216`); Drift-Test vergleicht nur gegen handgepflegte `EXPECTED_TOOL_NAMES`, nicht gegen `url_map` (125 Routen vs. 122 Tools). | `mcp_server.py:116-3216`, `tests/test_mcp_server.py:19,159` | (S) Paritätstest aus `app.url_map`; (M) Decorator `@api_tool(name, schema)` neben `@api_bp.post`, `TOOLS` aus Registry generieren | S/M |
| A3 | M | **Doppelte Validierung API/Web mit abweichenden Regeln.** Buchung: API übernimmt `status` vom Client, setzt kein `changed_by` (Audit = „system“), kennt kein `post_to_closing_period`; Web erzwingt `posted`, prüft Betrag > 0 und ≥ 2 Zeilen selbst. Konto: API nutzt `create_account_with_audit`, Web baut `Account(...)` inline; `account_type` wird nirgends gegen eine Liste geprüft. Anlage: Web castet IDs, API reicht roh durch. Beleg-Upload: Validierung dupliziert, `api/documents.py:32` importiert aus `app.web.helpers` (Schichtinversion), Persistenz 4× kopiert, Datei wird vor dem Commit geschrieben ohne `unlink` bei Fehler. `_api_changed_by` 16× definiert. | `api/journal.py:156-262` vs. `web/journal.py:278-418`; `api/accounts.py:52` vs. `web/accounts.py:84-98`; `api/fixed_assets.py:100-105`; `api/documents.py:67-83,216-262,334-380`; `web/helpers.py:169-192`; `web/documents.py:166-208` | Parser-Funktionen im Service, die Form- und JSON-Dicts gleich behandeln; `store_document()` im Service; `changed_by` in einem Helper | M |
| A4 | M | **Fehlermapping uneinheitlich, kein API-500-Handler.** 36 Fehlerklassen ohne gemeinsame Basis; `JournalEntryCreationError` → 422 in `api/journal.py:249`, 400 in `api/receipt_ocr.py:281`; `ValueError` → 400 (35×) vs. 422 (`api/fixed_assets.py:108`). Einziger `errorhandler`: `RequestEntityTooLarge`; unbehandelte Exceptions liefern HTML-500 an JSON-Clients. | `app/__init__.py:143` | `DomainError(code, http_status)` + ein `api_bp.errorhandler` | S |
| A5 | M | **Index-Drift Modell ↔ Migration.** Migration 0007 legt u. a. `ix_journal_entry_company_date`, `ix_journal_entry_line_account`, `ix_open_item_company_status_due` an; im ORM nur 2 `Index()` → `alembic autogenerate` würde sie droppen. | `models.py:552,1352`, `migrations/versions/20260706_0007*` | Indexe in `__table_args__` nachziehen | S |
| A6 | N | N+1 in Anlagenliste (je Anlage eine SUM-Query); `journal_page` (177 Zeilen, 8 Queries inline); `build_audit_export_package` (364 Zeilen) mischt Sammlung/Serialisierung/Hashing; `register_cli_commands` (308 Zeilen) mit `seed_demo` als Closure; Migration 0020 lädt alle `audit_log`-Zeilen in Python. | `api/fixed_assets.py:141`, `services/fixed_assets.py:437`, `web/journal.py:46`, `audit_export.py:199`, `cli.py:49,216` | GROUP-BY-Subselect; View-Model-Builder in Service; Modul-Split | S–M |
| A7 | N | **Dependencies/Runtime**: kein Lockfile (transitive Pakete ungepinnt, Docker-Build nicht reproduzierbar); `pyproject.toml` ohne `[project]`/`requires-python`; venv 3.10 vs. Docker/CI 3.12 (`date.fromisoformat` akzeptiert ab 3.11 mehr Formate → Prod validiert lockerer als lokale Tests). | `requirements.txt`, `pyproject.toml`, `.venv/pyvenv.cfg` | `requires-python = ">=3.12"`, `pip-compile`/uv-Lock, venv neu | S |

### 3.4 Tests und CI

| Nr. | Grad | Befund | Beleg | Fix | Aufw. |
|---|---|---|---|---|---|
| T1 | H | **Kein PostgreSQL in CI**; `psycopg` fehlt sogar im venv. Migration 0019 hat einen eigenen PG-Zweig `_upgrade_postgresql()`, der nie läuft; NUL-Byte-Sanitizing nur auf SQLite geprüft; `with_for_update()` ist auf SQLite ein No-op. | `ci.yml:24-27`, `20260713_0019_db_immutability.py:99,213-217`, `journal_entries.py:653,704`, `audit_log.py:156` | `services: postgres:17` + Matrix `DATABASE_URL ∈ {sqlite, postgresql}` | S |
| T2 | H | **FK-Constraints in App-SQLite aus**: Engine ohne `PRAGMA foreign_keys=ON`; nur Unit-Fixtures setzen es → App-Tests laufen ohne FK-Prüfung, `ondelete="CASCADE"` wirkt in SQLite nicht. | `app/db.py:88`, `test_journal_entries.py:23-28` | `connect`-Listener in `create_session_factory` (+ WAL) | S |
| T3 | H | Nummernkreis-Race ungetestet (siehe F6); Threading nur in `test_mcp_http.py`. | `journal_entries.py:622-629` | Test mit zwei Sessions gegen PG | S |
| T4 | H | Keine Coverage-Messung/-Schwelle (`coverage` nicht installiert). | `requirements-dev.txt`, `ci.yml` | `pytest-cov` + `--cov-fail-under` | S |
| T5 | M | Upload-Pollution: `DOCUMENT_UPLOAD_DIR` zeigt per Default auf `instance/uploads`; nach einem Testlauf liegen 117 Dateien in `instance/uploads/1/1`. Kein `conftest.py`, `_create_test_app` 16× dupliziert. | `app/__init__.py:34`, `test_app.py:33`, `test_chat.py:15`, `test_vat_returns.py:128` | zentrale Fixtures `app`/`client`/`session` mit `tmp_path` | S |
| T6 | M | Keine Typprüfung, kein `pip-audit`, kein Docker-Build-Test, kein Downgrade-Rundlauf in CI (funktioniert: up 0,66 s, down 0,42 s). | `ci.yml` | `mypy app domain`, `pip-audit`, `docker build .`, `alembic downgrade base && upgrade head` | S |
| T7 | M | Keine Golden-Files/XSD-Validierung für DATEV/SEPA/XRechnung (nur feldweise Asserts); Playwright in `requirements-dev` aber ungenutzt (nur `tools/screenshot_ui.py`). | `test_datev_export.py:93-113`, `test_sepa.py:114-126`, `test_einvoice_export.py:62-69` | `tests/golden/` + XSD-Check pain.001/UBL; Playwright entfernen oder Browser-E2E | M |
| T8 | N | Keine Property-based Tests für Rundung/Soll=Haben; nur 1× `parametrize`; `ci.yml` ohne pip-Cache, Matrix, `concurrency`, SHA-Pinning; `pytest` und `pytest -m e2e` laufen doppelt. Modul ohne direkten Test: `app/web/income_taxes.py`; echte HTTP-Schicht von `chat_llm`, `document_llm`, `mcp_client` ungetestet. | `ci.yml:22-24` | hypothesis für Rundung; CI aufräumen | S |

### 3.5 UI / UX

| Nr. | Grad | Befund | Beleg | Fix | Aufw. |
|---|---|---|---|---|---|
| U1 | H | **Eingaben gehen bei Fehlern verloren**: Fehler → Flash → Redirect, Formular leer (107× `flash(..., "error")`); keine Feldfehler, Pflichtfelder nur per `required`. | `web/journal.py:407-412` u. a. | bei Fehler `render_template` mit `request.form`, Fehlerklasse am Feld | M |
| U2 | H | **Kein deutsches Betragsformat**: `1.234,56` und `100,50` scheitern an `Decimal(value)`; Ausgabe roh, ISO-Datum überall (36× `isoformat()`), „€“ nur im Dashboard. Ein passender Parser existiert bereits in `receipt_ocr.py:460-463`. | `buchungen.html:55,272-273`, `journal_entries.py:410-414` | zentrales `parse_decimal` (Komma/Tausenderpunkt), Jinja-Filter `de_betrag`/`de_datum`, `inputmode="decimal"` | S |
| U3 | H | **Buchungsmaske nicht Vielbucher-tauglich**: kompletter Kontenplan als `<select>` je Zeile (3× im HTML), kein Autocomplete, keine Live-Summe Soll = Haben, keine Steuer-Vorschau, kein Duplizieren; `app.js` kann Zeilen nur anfügen, nicht entfernen; Template-Zeile hat `required` → leere Zusatzzeile blockiert Submit; Belegzuordnung nur auf separater Seite. | `buchungen.html:39-44,110-115`, `app.js:9-16` | Combobox (`<input list>`), Zeile löschen, JS-Summenzeile, „Speichern & Kopie“ | M |
| U4 | M | **Bestätigungen fehlen** bei Einzel-Festschreiben, Periode sperren/entsperren, Anlagen-Storno/Abgang, Lohnlauf buchen, Token rotieren, ELSTER-Test. | `buchungen.html:249`, `perioden.html:112-123`, `anlagen.html:235,265`, `lohn.html:184`, `verwaltung.html:111`, `ustva.html:238` | `data-confirm` ergänzen | S |
| U5 | M | Pagination/Sortierung nur auf 4 Seiten; Kontenplan unpaginiert; Audit-Log nur `limit ≤ 500` ohne Offset; keine Spaltensortierung; OPOS filtert/paginiert in Python; Bulk nur im Zahllauf. | `konten.html:67`, `audit_log.py:20-21`, `open_items.py:85-101` | Makro überall, Sortier-Parameter, „alle auswählen“ | M |
| U6 | M | **Dashboard ohne Geschäftsführer-Sicht**: GuV/Bilanz ohne Zeitraum („Jahresergebnis“ = Gesamthistorie); keine Liquidität/Bankstand, Forderungen/Verbindlichkeiten nach Fälligkeit, Umsatz je Monat, USt-Zahllast + Termin, nicht festgeschriebene Buchungen. | `dashboard.py:93-98`, `reports.py:111-116` | aktuelles WJ durchreichen; Kacheln ergänzen | M |
| U7 | M | Bank-Umsatztabelle: bis zu 3 Formulare und 6 Selects je Zeile; fehlende CSS-Klassen (`grid-2`, `list-item`, `action-row`, `label.inline`, `strong.negative` → Mitarbeiterformular ungestylt, „Nein“ nicht rot); Terminologie-Mix („Gegenseite“/„Gegenpartei“/„Debitor“), englische Rohwerte (`asset`, `posted`); 16× `style="margin:0;"`. | `bank.html:359-441`, `lohn.html:43,73,169`, `anlagen.html:83,211`, `berichte.html:105`, `konten.html:23-29,83` | Aktionspanel je Zeile, `datalist`; Klassen anlegen; Label-Mapping | S–M |
| U8 | M | **Accessibility**: Labels ohne `for` in Buchungs- und E-Rechnungsmaske; 0× `scope=` in Tabellen; kein Skip-Link; Dropdown ohne `aria-expanded`; Chat-Löschen-Button nur bei Hover sichtbar (Tastatur unerreichbar), `outline:none` auf Textarea; Fokusstil nur für `input/select`; `.login-card{width:380px}` läuft unter 400 px über. | `buchungen.html:38-81`, `erechnung.html:114-160`, `base.html:55`, `chat.css:91-96,352`, `style.css:258-262,548` | Standard-A11y-Pass | S |
| U9 | N | `chat.js`: `response.json()` bei Session-Ablauf (HTML) → `SyntaxError` als Fehlertext; kein Timeout/Abort; kein Streaming; Rendering doppelt (Template + JS); `setTimeout`-Hack gegen `app.js`. `app.js` deaktiviert Submit-Buttons dauerhaft → nach Datei-Downloads (E-Rechnung, Zahllauf) bleibt der Button tot. `style-src 'unsafe-inline'` wegen 73 Inline-Styles. | `chat.js:140-143,184`, `app.js:42-48`, `einvoice.py:317`, `payment_run.py:102` | Content-Type prüfen; Buttons per `pageshow` reaktivieren; Inline-Styles in CSS | S |
| U10 | N | Navigation: 7 Top-Level-Einträge, 20 Unterpunkte; „Anlagen“/„Lohn“ unter „Bank & OPOS“; keine globale Suche; Gesellschaftswechsel verwirft Filter (`action="{{ request.path }}"`). | `base.html:15-51,77` | Gruppen „Abschluss & Steuern“/„Stammdaten“; Query-String erhalten | S |

**API ohne UI:** Steuercodes anlegen/Defaults (`/tax-codes`, `/tax-codes/defaults`),
Kontoblatt `/accounts/<id>/history` (UI zeigt nur Audit-Events), Vorlagen
anlegen/reaktivieren ohne Buchung.

### 3.6 Betrieb und Deployment

| Nr. | Grad | Befund | Beleg | Fix | Aufw. |
|---|---|---|---|---|---|
| O1 | H | **`redeploy.sh` rollt die Dev-Compose-Datei aus**: plain `docker compose down/build/up` liest `docker-compose.yml` (Flask-Dev-Server `python run.py`, Bind-Mount `./:/app`, `APP_ENV=development`, DB-Passwort `openbuchhaltung`), nicht `docker-compose.production.yml`, wie README:174 verlangt. Beide teilen Projektname und Volume. | `redeploy.sh:29-35`, `docker-compose.yml:4,11,13,86` | `-f docker-compose.production.yml` fest verdrahten; Prod-Compose als einzige Deploy-Datei | S |
| O2 | H | **Startup-Migration ohne Sperre bei 2 Workern**: kein `--preload`, jeder Worker ruft `create_app()` → `_bootstrap_schema` → `alembic upgrade head`; kein Advisory-/File-Lock; zweiter Worker scheitert → Boot-Fehler → Container-Neustart. Migration läuft außerdem bei jedem CLI-Aufruf (`verify-integrity`). | `docker-compose.production.yml:4`, `app/__init__.py:116,121`, `app/db.py:45-84` | `alembic upgrade head` als eigener Schritt im Redeploy; Auto-Migration in Prod abschaltbar; alternativ `pg_advisory_xact_lock` | M |
| O3 | H | **gunicorn ohne `--timeout`** (30 s) vs. synchrone Langläufer: KI-Chat bis 15 Tool-Calls × 120 s LLM-Timeout, OCR 2×30 s, Belegabgleich 30 s, FinTS ohne Timeout, Audit-ZIP komplett im RAM → SIGKILL, 502, Worker-Neustart. | `app/__init__.py:72-74`, `chat.py:468`, `receipt_ocr.py:301,740`, `receipt_matching.py:224`, `fints_sync.py:68`, `audit_export.py:468-471` | kurzfristig `--timeout 180 --graceful-timeout 30`; mittelfristig Job-Queue | S / L |
| O4 | H | **Kein `.dockerignore`**: `COPY . .` nimmt `instance/` (DB 956 KB, `uploads/1`, `uploads/2` = echte Belege), `.git`, `tests`, `.claude/` mit ins Image; in Prod überdeckt das Volume, die Daten bleiben aber im Layer (Registry, `docker save`). | `Dockerfile:8` | `.dockerignore` mit `instance/ .git .venv tests .claude __pycache__` | S |
| O5 | H | **Kein Backup/Restore**: kein pg_dump-Skript, kein Cron, keine Anleitung; `produktionsbetrieb.md` §6 fordert es nur. Redeploy macht `compose down` ohne Backup, danach Auto-Migration ohne Rollback-Pfad, `git pull` ohne Tag. | `redeploy.sh:26-29`, `docs/compliance/produktionsbetrieb.md:76-94` | `backup.sh` (pg_dump + tar uploads + Revision), vor `down` aufrufen; Restore-Runbook | M |
| O6 | M | Dockerfile: root, kein `HEALTHCHECK`, kein Digest-Pin, `CMD python run.py` (Dev-Server); Health-Endpoint statisch ohne DB-Check; kein `healthcheck` für `app` im Compose. | `Dockerfile:1-11`, `run.py:10`, `api/system.py:10-13`, `production.yml:30-34` | `USER`, HEALTHCHECK, `SELECT 1` + Alembic-Revision + Version im Health | S |
| O7 | M | **Logging**: keine `basicConfig`/`dictConfig` → `app.*`-Logger ohne Handler; `migrations/env.py:23-24` ruft `fileConfig()` mit `disable_existing_loggers=True` → nach einer Startup-Migration sind alle `app.*`-Logger für die Prozesslebensdauer stumm. Keine Request-ID, kein Fehler-Reporting, keine Metriken. | `migrations/env.py:23-24`, `app/db.py:84` | `dictConfig` in `create_app`, `fileConfig(..., disable_existing_loggers=False)`, Request-ID-Middleware | S |
| O8 | M | Kein ProxyFix, kein `--forwarded-allow-ips` (siehe S4); Engine ohne `pool_pre_ping`/`pool_recycle` → nach DB-Neustart tote Verbindungen; SQLite ohne FK-Pragma/WAL (siehe T2). | `app/db.py:88` | `pool_pre_ping=True`; Connect-Listener | S |
| O9 | M | **Keine Versionierung**: 0 Tags, kein CHANGELOG, keine Versionsnummer; `produktionsbetrieb.md` fordert „Softwareversion und Commit“ und „Release Notes lesen“. Keine ENV-Referenz (≥ 37 Variablen verstreut, kein `.env.example`; `CSRF_PROTECT`, `FLASK_ENV`, `SELLER_COUNTRY_CODE` undokumentiert). | `produktionsbetrieb.md:51,100` | SemVer-Tags, CHANGELOG, Version im Health/Footer, `.env.example` | M |
| O10 | N | Caddy nur vor `/mcp`, keine Security-Header/Rate-Limit, IP-Allowlist auskommentiert; `verify-integrity` nicht automatisiert; `worker`-Service ist toter Platzhalter (`python -m http.server`). | `Caddyfile:16-35`, `docker-compose.yml:25-34` | Allowlist aktivieren; Timer-Service für Integritätsprüfung; Platzhalter entfernen | S |

## 4. Feature-Lücken

### 4.1 Feature-Matrix (Zielgruppe: kleine GmbH, Buchhaltung selbst)

| Bereich | Feature | Status | Anmerkung |
|---|---|---|---|
| Stammdaten | Kunden/Debitoren, Lieferanten/Kreditoren | fehlt | nur Freitext `OpenItem.counterparty` (+ IBAN), kein Modell |
| Stammdaten | Eigene Firmendaten (Adresse, USt-IdNr, Steuernummer) | fehlt | `Company` hat nur name/currency/iban/bic; Verkäuferdaten per `SELLER_*`-ENV |
| Stammdaten | Artikel/Leistungen, Nummernkreise (Rechnung/Kunde) | fehlt | nur Buchungsnummer |
| Verkauf | Ausgangsrechnung erstellen | teilweise | XRechnung/ZUGFeRD-XML aus Formular, nicht persistiert, keine Buchung/OPOS, kein PDF |
| Verkauf | Angebote, Gutschriften, wiederkehrende Rechnungen, Anzahlung/Schlussrechnung | fehlt | nur wiederkehrende *Buchungen* (`JournalTemplate`) |
| Verkauf | Versand per E-Mail, Mahnung als PDF/Mail, Mahngebühren/Zinsen | fehlt | Mahnschreiben nur als druckbare HTML-Seite; kein SMTP |
| Einkauf | Eingangsrechnung → OPOS mit Fälligkeit, Freigabe-Workflow | teilweise | OCR/Abgleich-Vorschlag mit Status; `incoming_invoice.py` legt keinen OPOS an |
| Einkauf | Belegeingang per E-Mail, Kassenbuch, Reisekosten | fehlt | |
| Buchhaltung | Splitbuchung aus Bankumsatz | teilweise | Journal mehrzeilig; Bankumsatz nur ein Gegenkonto |
| Buchhaltung | Fremdwährung, Kontenrahmenwechsel | fehlt | `currency_code` je Zeile ohne Kurse; FW-Umsätze werden abgewiesen |
| Buchhaltung | ARAP/PRAP, Rückstellungen, Anzahlungen, Skonto, 1 %-Regelung | fehlt | |
| Buchhaltung | Ist-Versteuerung, Kleinunternehmer, Dauerfristverlängerung | fehlt | |
| Buchhaltung | § 13b, ZM, OSS, innergemeinschaftliche Vorgänge | fehlt | `TaxCode` nur rate + Konto; UStVA nur Kz 81/86/48/66/83/35 |
| Abschluss | UStVA/USt-Jahr, KSt/GewSt, Jahresabschluss, AfA | vorhanden | mit Befunden F1, F7, F9, F10 |
| Abschluss | E-Bilanz (XBRL), Bundesanzeiger, Anhang | fehlt | |
| Abschluss | GuV nach § 275 HGB (GKV/UKV), BWA, Vorjahresvergleich | fehlt / teilweise | nur Erlöse − Aufwand nach Kontotyp |
| Abschluss | Liquiditätsplanung, Budget/Soll-Ist | fehlt | Plan Phase 4.7 offen |
| Zusammenarbeit | Steuerberater-Rolle/Einladung, DATEV-Import, Notizen an Buchungen, E-Mail-Benachrichtigung, i18n | fehlt | Pruefer-Rolle read-only vorhanden |
| Zusammenarbeit | Mobile/PWA, Belegfoto | teilweise | 2 Media-Queries; JPG-Upload ohne `capture` |
| Plattform | 2FA/Passkeys/SSO, Passwort ändern | fehlt | Auth-Routen nur login/logout |
| Plattform | Backup/Restore, Volltextsuche Belege, Webhooks, Hintergrundjobs, Konsolidierung | fehlt | Suche nur Dateiname; AfA je Anlage einzeln |

### 4.2 Top-15 fehlende Features (nach Nutzen für die Zielgruppe)

| # | Feature | Nutzen | Aufw. | Andockpunkt |
|---|---|---|---|---|
| 1 | Kunden-/Lieferantenstamm (Adresse, USt-IdNr, Zahlungsziel, IBAN) | Basis für Rechnung, OPOS, Mahnung, SEPA ohne Freitext | M | neue Entität; `open_items.py` (counterparty → FK); `einvoice_export.Party` |
| 2 | Ausgangsrechnungsmodul (Entität, Nummernkreis, PDF + XRechnung, Buchung + OPOS automatisch) | häufigster Alltagsfall, heute nur XML-Download | L | `einvoice_export.py`, `web/einvoice.py`, `journal_entries.py`, `open_items.py` |
| 3 | Firmenstammdaten in DB statt `SELLER_*`-ENV | Pflicht für Rechnung, ELSTER, Mahnung | S | `Company`, `web/admin.py`, `app/__init__.py:93` |
| 4 | § 13b / innergemeinschaftlicher Erwerb / ZM (Kz 21/41/46/47/89) | fast jede GmbH hat EU-/SaaS-Eingangsrechnungen | M | `TaxCode` (Typ/Reverse-Flag), `vat_returns.py:286ff`, `elster.py` |
| 5 | Eingangsrechnung → OPOS mit Fälligkeit | Zahllauf/Mahnwesen nur so nutzbar | S | `incoming_invoice.py` + `open_items.create` |
| 6 | E-Mail-Versand (Rechnung, Mahnung) + Mahn-PDF | Prozess ohne Medienbruch | M | `services/mailer.py`, `web/dunning.py` |
| 7 | Gutschriften/Rechnungskorrektur | rechtlich nötig nach 2 | S | Rechnungsmodul |
| 8 | BWA (DATEV-Schema) + Vorjahresvergleich | Standard-Report für GF/Bank | M | `reports.py:account_balances_by_type`, `berichte.html` |
| 9 | Kassenbuch (GoBD, fortlaufend, Tagessaldo) | Bargeld-Compliance | M | Modell analog `BankTransaction` |
| 10 | Hintergrundjobs (Vorlagen buchen, AfA-Jahreslauf, Mahnlauf, FinTS-Abruf) | weniger Klicks, weniger Vergessen | M | cron-fähige CLI-Kommandos; Schleife über `post_depreciation` |
| 11 | Skonto-Automatik beim OPOS-Ausgleich | Differenzen sonst Handbuchung | S | `open_items.py:settle` |
| 12 | Notizen/Kommentare an Buchung und Beleg + Steuerberater-Rolle | Rückfragen im System statt per Mail | M | neues Modell, `web/journal.py`, `auth.py ROLES` |
| 13 | 2FA (TOTP) + Passwort ändern | Erwartungsstandard bei Finanzdaten | S/M | `auth.py:296-344`, `User` |
| 14 | Fremdwährung (Kurs, Kursdifferenz) | USD-SaaS, Auslandseinkauf | L | `JournalEntryLine`, `bank_import.py`, Reports |
| 15 | Ist-Versteuerung / Kleinunternehmer-Modus | viele UGs starten so | M | Company-Flag, `vat_returns.py` |

### 4.3 Paritätslücken UI / API / MCP

MCP spiegelt die API bis auf `DELETE /documents/<id>` (antwortet ohnehin 409),
`GET /documents/<id>/content` und `POST /mcp/call` — konsistent.

- **Nur UI:** FinTS-TAN-Dialog abbrechen (`web/bank.py:608`); Dashboard-
  Kennzahlen/offene Aufgaben (`web/dashboard.py:29`); Mahnschreiben-Rendering.
- **Nur API/MCP:** Steuercodes anlegen/Defaults; `GET /documents/<id>/content`.
- **In allen drei Schichten fehlend:** Gesellschaft bearbeiten (Name/Währung),
  Mitarbeiter/OPOS/Steuercodes ändern, Passwort ändern, weitere Gesellschaft
  zu bestehendem Mandanten anlegen (`tenants.py` nur `POST /tenants`).

## 5. Was gut ist

- **Ein Buchungspfad.** Alle 13 buchenden Stellen (Bank, OCR, E-Rechnung,
  Lohn, AfA, Vorlagen, Eröffnung, Storno, Abschluss) rufen
  `create_journal_entry`; `JournalEntry(` existiert nur dort; kein
  Update-/Delete-Pfad; MCP läuft über die REST-API. Soll = Haben, Kontoaktivität,
  Periodensperre, WJ-Status und Controlling-Gültigkeit werden zentral geprüft;
  DB-CHECKs sichern Single-Side, `UNIQUE(reversal_of_id)` verhindert Doppelstorno.
- **GoBD-Kern.** SHA-256-Hashkette mit Tenant-Anker und `FOR UPDATE`,
  Inhalts-Hash v2 festgeschriebener Buchungen inkl. Zeilen, Append-only-Trigger
  (Migrationen 0019/0020), Storno-Prinzip, Stornos sofort festgeschrieben.
- **Rundung und Rechenkerne.** Durchgängig `Decimal`/`ROUND_HALF_UP`; AfA pro
  rata temporis mit Rundungsrest im letzten Jahr und degressiv→linear-Wechsel;
  GewSt-Abrundung auf 100 €; IBAN Mod-97; pain.001-Struktur korrekt.
- **Sicherheitsbasis.** Tenant-Scoping konsequent über `api_scoped_company` /
  `require_company_access`; Templates ohne `|safe`/Inline-Skripte, CSP
  `script-src 'self'`, `chat.js` nur `textContent`; Upload-Allowlist mit
  Magic-Bytes, Größenlimits, Ablage außerhalb des Web-Roots; scrypt-Passwörter,
  SHA-256-Token-Lookup, timing-sichere Vergleiche, CSRF auf allen UI-POSTs,
  `SECRET_KEY`-Pflicht in Produktion; FinTS-PIN/TAN nie persistiert.
- **Architektur.** Echte Service-Schicht mit expliziter `session` und
  Eingabe-Dataclasses; `commit`-Flag als Muster vorhanden; Reports aggregieren
  in SQL; MCP-Server stdlib-only und mit Fake-Client testbar; zentrale Config
  in der Factory; Auto-Migration mit Schutz externer DBs.
- **Testsuite.** Saubere Isolation (eigene SQLite je App), Netzwerk komplett
  gemockt (FinTS, LLM, ELSTER), DB-Schutzmechanismen und Hashketten inkl.
  Manipulationserkennung getestet, Paritätstest MCP ↔ API, robuste Asserts
  ohne Zeitbrüchigkeit, 65 s Laufzeit.
- **Betrieb.** Fail-fast bei fehlenden Secrets, MCP verweigert Nicht-Loopback
  ohne Token, alle Ports nur auf `127.0.0.1`; 36 Migrationen mit echten
  Downgrades; Betriebsanforderungen sind formuliert, es fehlt die Umsetzung.

## 6. Priorisierter Umsetzungsvorschlag

Jeder Sprint ist so geschnitten, dass er in einem PR mit grüner CI landen kann.

**Sprint 1 – Fachliche Korrektheit (ca. 5–7 Tage)**
1. F1: Abschlussbuchungen in GuV/Bilanz/UStVA/KSt ausklammern (Schalter),
   Test in `test_periods.py` umdrehen, Saldovortrag ins Folgejahr (F7).
2. F2: Netto-aus-Brutto mit Rundungszeile.
3. A1 + F11: `commit=False` in AfA, Lohn, Belegabgleich, Storno.
4. F5: `TaxCode.kind` (input/output) + Validierung.
5. F6: Buchungsnummer je WJ mit Sequenzzeile und Retry.
6. F4: Storno-Hooks für Bank, AfA, OPOS, Lohn.
7. F12/F13: Cent-Quantisierung, Storno-Datum, WJ-Überlappung.

**Sprint 2 – Sicherheit (ca. 3–4 Tage)**
1. S1: Chat-Tool-Allowlist (lesend), Human-in-the-Loop für schreibende Tools,
   Benutzer-/Token-/ELSTER-/SEPA-/FinTS-Tools raus; Anhänge als Daten kapseln.
2. S2: Secret-Redaktion in `tool_calls`.
3. S3: `/mcp/call` entfernen oder in-process mit Aufruferkontext.
4. S4/O8: ProxyFix, Rate-Limit in DB; S6 Open Redirect; S7 Cookie/HSTS;
   S8 Admin-Check; S9 Body-Limit; S10-Einzeiler.
5. Login-/Admin-Audit-Events; Passwort-ändern-Flow.

**Sprint 3 – Betrieb (ca. 3–4 Tage)**
1. O1: `redeploy.sh` auf Prod-Compose; `.dockerignore` (O4); Dockerfile
   non-root + HEALTHCHECK + gunicorn-CMD (O6).
2. O5: `backup.sh`/`restore`-Runbook, Backup vor Redeploy.
3. O2: Migration als eigener Schritt, Auto-Migration in Prod abschaltbar.
4. O3: `--timeout 180`; O7 Logging-Konfiguration + `disable_existing_loggers=False`;
   Health mit DB-Check und Version.
5. O9: Tags + CHANGELOG + `.env.example`.

**Sprint 4 – Qualität (ca. 3 Tage)**
1. T1: Postgres in CI (Matrix); T2 FK-Pragma + WAL; T4 Coverage-Gate.
2. T5: `conftest.py` mit `tmp_path`-Uploads; T6 mypy/pip-audit/Docker-Build/
   Downgrade-Rundlauf; T8 CI aufräumen.
3. A2: Paritätstest aus `url_map`; A4 `DomainError` + Errorhandler;
   A5 Index-Drift; A7 Lockfile + `requires-python`.
4. F3 + T7: DATEV je WJ mit BU-Schlüsseln und Golden-File; SEPA/UBL-XSD.

**Sprint 5 – Produkt-Basis (ca. 8–12 Tage)**
1. Firmenstammdaten in DB (#3), Kunden-/Lieferantenstamm (#1).
2. Eingangsrechnung → OPOS (#5), Skonto (#11), Zahllauf markiert Posten (F8).
3. § 13b / ig. Erwerb / ZM (#4) inkl. UStVA-Kennziffern (F10).
4. BWA + Vorjahresvergleich (#8).
5. Steuercode-UI, Gesellschaft bearbeiten, weitere Gesellschaft je Mandant (4.3).

**Sprint 6 – UX (ca. 4–5 Tage)**
1. U2: deutsches Betrags-/Datumsformat (Parser + Filter).
2. U1: Formular-Repopulation und Feldfehler.
3. U3: Buchungsmaske (Combobox, Zeile löschen, Live-Summe, Kopie).
4. U4: Bestätigungsdialoge; U7 CSS-Klassen/Terminologie; U8 A11y-Pass.
5. U6: Dashboard mit WJ-Bezug, Liquidität, Fälligkeiten, USt-Zahllast.

Danach: Ausgangsrechnungsmodul (#2, #6, #7), Hintergrundjobs (#10),
Kassenbuch (#9), 2FA (#13), Ist-Versteuerung (#15), Fremdwährung (#14).
