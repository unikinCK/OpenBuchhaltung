# Changelog

Alle nennenswerten Änderungen an OpenBuchhaltung werden hier festgehalten.
Das Format folgt [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), die
Versionierung [SemVer](https://semver.org/lang/de/). Releases tragen den Git-Tag
`v<Version>`; die Version steht in `pyproject.toml` (`[project] version`) und wird
über `GET /api/v1/health` sowie im Prüferexport-Manifest ausgegeben.

## [Unreleased]

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
