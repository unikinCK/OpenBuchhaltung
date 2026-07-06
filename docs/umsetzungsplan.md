# Umsetzungsplan: OpenBuchhaltung (UG, GmbH, gGmbH)

## 1. Zielbild
OpenBuchhaltung wird eine webbasierte Open-Source-Buchhaltungssoftware für deutsche Kapitalgesellschaften
(UG, GmbH, gGmbH) mit Fokus auf HGB-Konformität, Nachvollziehbarkeit und Erweiterbarkeit.

**Technologie-Stack (Start):**
- Backend: Python + Flask
- Datenbank: SQLite (Entwicklung/Einzelmandant), optional PostgreSQL oder MariaDB (Produktion)
- Frontend: Flask-Templates + HTMX/Alpine.js (später optional SPA)
- Hintergrundjobs: Celery/RQ (für Export, OCR, E-Mail, Prüfungen)

## 2. Einordnung von GnuCash (Desktop) als Referenz
GnuCash ist stark in der doppelten Buchführung, aber für den geplanten Web-/HGB-Fokus sind folgende Punkte relevant:
- primär als Desktop-Anwendung gedacht
- SQL-Speicher möglich (SQLite/MySQL/PostgreSQL), aber ohne echte Multi-User-DBMS-Funktionalität
- kein Schwerpunkt auf deutscher HGB-Standardisierung für Kapitalgesellschaften

=> Für OpenBuchhaltung sollte der Fokus auf Mandantenfähigkeit, rollenbasierter Zusammenarbeit,
   GoBD-konformer Historisierung/Audit-Log sowie HGB-Berichtswesen liegen.

## 3. Fachlicher Scope (MVP bis V2)

### MVP (erste produktive Version)
1. Mandanten- und Stammdatenverwaltung (UG, GmbH, gGmbH)
2. Kontenrahmen (SKR03/SKR04) inkl. anpassbarer Konten
3. Journalbuchungen mit Soll/Haben-Prüfung
4. Belegverwaltung (Upload, Verknüpfung mit Buchung)
5. USt-Logik (19%, 7%, steuerfrei, innergemeinschaftlich Basisfälle)
6. Standardauswertungen:
   - Summen- und Saldenliste
   - BWA (einfach)
   - Bilanz und GuV (HGB-Grundschema)
7. Abschlussfunktionen:
   - Periodensperre
   - Abschlussbuchungen (manuell unterstützt)
8. Export:
   - CSV
   - DATEV-ähnlicher Export (zunächst minimaler Umfang)
9. Rechte & Sicherheit:
   - Rollen (Admin, Buchhalter, Prüfer/Leser)
   - Vollständiger Audit-Log

### V1.5
- Offene-Posten-Logik Debitor/Kreditor
- Zahlungsabgleich (CSV-Import Bankumsätze)
- Mahnstufen (Basis)
- Anlagenverzeichnis (Basis)

### V2
- E-Rechnung (XRechnung/ZUGFeRD Import/Export)
- Automatisierte Belegerkennung (OCR + Buchungsvorschläge)
- Konsolidierung/mehrere Gesellschaften
- API für Steuerberater-Tools

## 4. Zielarchitektur

## 4.1 Schichten
1. **Presentation Layer**: Flask Blueprints (UI + API)
2. **Application Layer**: Use-Cases (Buchung erfassen, Abschlusslauf etc.)
3. **Domain Layer**: Fachobjekte (Konto, Buchung, Beleg, Periode, Steuercode)
4. **Persistence Layer**: SQLAlchemy + Alembic

## 4.2 Mandantenfähigkeit
- Jede Tabelle enthält `tenant_id`
- Strikte Tenant-Filterung im ORM
- Optional später: physische Trennung je Mandant (eigene DB)

## 4.3 Auditierbarkeit/GoBD-Basis
- Unveränderbarkeit von Buchungen nach Festschreibung
- Korrekturen ausschließlich über Storno-/Gegenbuchungen
- Lückenlose Änderungsprotokolle (wer, wann, was)
- Versionierte Reports (Hash über Report-Inhalt und Parameter)

## 4.4 Datenbankstrategie
- **Entwicklung:** SQLite
- **Produktion default:** PostgreSQL
- **Alternative:** MariaDB

Hinweis: Datenbankspezifische SQL-Features zunächst vermeiden (portable SQLAlchemy-Nutzung),
um Wechsel zwischen Engines zu vereinfachen.

## 5. Domänenmodell (Kern-Entitäten)
- `Tenant` (Mandant)
- `Company` (Gesellschaftsdaten, Rechtsform, Geschäftsjahr)
- `FiscalYear`, `Period`, `PeriodLock`
- `Account` (inkl. Kontenklasse, SKR-Mapping)
- `TaxCode` (Steuerlogik)
- `JournalEntry`, `JournalEntryLine`
- `Document` (Belegmetadaten + Datei)
- `VatReturn` (Vorbereitung UStVA)
- `ReportSnapshot`
- `User`, `Role`, `Permission`, `AuditLog`

## 6. Sicherheits- und Compliance-Anforderungen
- DSGVO: Datenminimierung, Export/Löschkonzepte
- IT-Sicherheit:
  - Passwort-Hashing (Argon2/Bcrypt)
  - CSRF-Schutz
  - Rate Limiting
  - Verschlüsselung ruhender Belege (optional in V1)
- Backups:
  - automatisierte tägliche Backups
  - Restore-Test als Pflichtprozess

## 7. Projektphasen mit konkreten Tasks

## Phase 0 – Foundations (2–3 Wochen)
- [x] Repository-Struktur aufsetzen (`app/`, `domain/`, `tests/`, `migrations/`)
- [x] Docker-Compose (app + db + worker + adminer/pgadmin)
- [x] CI (Lint, Tests, Migrationscheck)
- [x] Coding-Guidelines + ADR-Template
- [x] Grundlegendes Rechtemodell + Login (v0, Demo-User)

## Phase 1 – Kernbuchhaltung MVP (6–10 Wochen)
- [x] Kontenrahmenimport SKR03/SKR04
- [x] Buchungsmaske (Soll/Haben, Steuercode, Beleglink) *(MVP-Basis umgesetzt; Beleglink folgt mit P1-002)*
- [x] Validierungsregeln (Bilanzgleichheit, gesperrte Perioden) *(Basisregeln inkl. Periodensperre umgesetzt)*
- [x] Belegupload + Speicherung + Verknüpfung
- [x] Externes LLM für Beleg-Updates über OpenAI-Responses-kompatible Schnittstelle integrieren *(Upload-Flow ruft optional einen OpenAI-Responses-kompatiblen Endpoint auf; Fehler blockieren Upload nicht)*
- [x] Audit-Log für alle buchungsrelevanten Aktionen *(für JournalEntry-Erfassung umgesetzt; Erweiterung siehe P1-005)*
- [x] Summen-/Saldenliste
- [x] GuV/Bilanz-Report (HGB-Basisschema) *(MVP-Basis mit GuV/Bilanz-Endpunkten, Bilanzgleichheitsindikator und UI-Totals umgesetzt)*
- [x] CSV-Export *(Core-Exports für Journal und Summen-/Saldenliste über API + UI-Downloadlinks umgesetzt)*
- [x] End-to-End-Tests für Kernflows *(Happy Path + fachliche Negativfälle und CI-Gate mit `pytest -m e2e` ergänzt)*

## Phase 1.5 – Prototyp-Härtung / Sprint C (Stand 2026-07-05)

Ziel: Aus dem funktionierenden Kern einen vorzeigbaren, von Dritten nutzbaren Prototyp machen.

- [x] **P1.5-001 Login-Pflicht durchsetzen**: UI-Routen erfordern Anmeldung; DB-Modell
      `User` mit Passwort-Hash (werkzeug/scrypt) ersetzt den Platzhalter-Userstore;
      Rollen (Admin/Buchhalter/Prüfer) werden bei Schreibaktionen geprüft. API optional
      per `API_AUTH_TOKEN` (Bearer) geschützt; Benutzer-Tokens folgen in Phase 3.
- [x] **P1.5-002 Tenant-Scoping aktivieren**: Session-Tenant des Benutzers filtert alle
      UI-Queries; Cross-Tenant-Zugriffe liefern 404; Mandanten anlegen nur als globaler
      Admin. Tests für Cross-Tenant-Verbot ergänzt.
- [x] **P1.5-003 Steuercodes in Buchungsmaske**: Standard-Steuercodes (USt19/USt7/VSt19/
      VSt7/frei) je Gesellschaft, Auswahl je Buchungszeile, automatische USt-/VSt-Teilbuchung
      (Netto-Erfassung); `TaxCode.vat_account_id` per Migration ergänzt.
- [x] **P1.5-004 Demo-Seed-Command**: `flask seed-demo` legt Mandant, Gesellschaft,
      SKR03-Konten, Steuercodes, Benutzer und Beispielbuchungen idempotent an.
- [x] **P1.5-005 Kleinigkeiten**: Port per `PORT`-ENV konfigurierbar (Default 8000),
      README auf Port 8000 umgestellt; `create_app` liest jetzt ENV-Variablen
      (DATABASE_URL, DOCUMENT_LLM_*, MCP_SERVER_URL, SECRET_KEY, API_AUTH_TOKEN) —
      vorher waren die dokumentierten Exports wirkungslos. Bugfix: GuV/Bilanz erkennen
      jetzt auch `account_type=income` (SKR-Importe) als Erlöskonten.

## Phase 1.6 – UI & Sicherheit / Sprint D (Stand 2026-07-05)

- [x] **P1.6-001 CSRF-Schutz**: Session-basierter Token für alle UI-/Auth-Formulare
      (ohne neue Dependency); Requests ohne gültigen Token liefern 400.
      In Tests standardmäßig deaktiviert, dedizierter CSRF-Test vorhanden.
- [x] **P1.6-002 Mehrseitige UI**: Monolithische Startseite aufgeteilt in Dashboard,
      Buchungen, Konten, Belege, Berichte und Verwaltung; Topbar-Navigation mit
      Gesellschaftsauswahl und Login-Status; eigenes CSS (`app/static/style.css`,
      kein Framework); Dashboard mit Kennzahlen (GuV-Totals, Bilanzsumme,
      Bilanzgleichheit, Zähler); GuV/Bilanz jetzt mit Einzelpositionen;
      Kontotyp-Auswahl statt Freitext; gestylte Login-Seite.

## Phase 2 – Prozesse & Qualität (4–6 Wochen)
- [x] Jahresabschluss-Workflow (Periodenabschluss + Ergebnisvortrag) *(Sprint E:
      Perioden-Seite mit Sperren [Schreibrollen] / Entsperren [nur Admin],
      Geschäftsjahr abschließen [nur Admin, sperrt alle Perioden], Buchungssperre für
      geschlossene Jahre, Audit-Events für alle Aktionen. Sprint J: Ergebnisvortrag —
      der Abschluss bucht die GuV-Konten gegen den Gewinnvortrag [SKR03 0860 / SKR04
      2970] glatt, bevor die Perioden gesperrt werden)*
- [x] OPOS-Basis Debitor/Kreditor *(Sprint G: Offene-Posten-Tabelle mit Debitor/
      Kreditor-Typ, Verknüpfung zu Konto/Buchung, Teil-/Vollausgleich gegen Bankumsatz
      oder Zahlungsbuchung, UI-Seite und Audit-Events umgesetzt)*
- [x] Bank-CSV-Import + Matching-Regeln *(Sprint F: CSV-Import mit Header-Aliassen,
      deutschem Zahlen-/Datumsformat und Dedup-Hash; Betrags-Matching schlägt passende
      Buchungen vor; offene Umsätze direkt verbuchbar inkl. Netto-aus-Brutto-Split
      bei Steuercode; Audit-Events für Import/Zuordnung/Verbuchung)*
- [x] Performance-Profiling großer Journaldaten *(Sprint H: CI-freundliche
      Performance-Baseline mit synthetischen Journaldaten, Reports, OPOS und
      Bank-Matching ergänzt; Index-Migration für zentrale Query-Pfade umgesetzt)*
- [x] Security-Hardening + PenTest-Light *(Sprint I: Security-Header,
      gehärtete Session-Cookies, Upload-Allowlist/Größenlimit und Tests für
      Header, Cookies, CSRF/Auth-Scoping sowie Upload-Missbrauch umgesetzt)*

## Phase 3 – Ökosystem & Automatisierung (6–12 Wochen)
- [x] DATEV-kompatiblere Exporte ausbauen *(Sprint K: DATEV-Buchungsstapel im
      EXTF-Format [Kopfzeile + Spaltenüberschrift + Buchungssätze, Windows-1252];
      2-zeilige Buchungen als Konto/Gegenkonto, mehrzeilige als Splitbuchung über
      Belegfeld 1; API `GET /api/v1/exports/datev.csv` + Download auf Berichte-Seite;
      Berater-/Mandantennummer konfigurierbar. Nicht zertifiziert, ohne BU-Automatik)*
- [ ] E-Rechnung Import/Export
- [ ] OCR-Pipeline für Belege
- [ ] REST-API + API-Tokens *(erstes Security-Inkrement: `API_REQUIRE_AUTH`,
      Benutzer-API-Tokens per CLI, Tenant-Scoping und Rollenprüfung für bestehende
      API-Endpunkte umgesetzt; weiterer API-Ausbau offen)*
- [ ] Mandantenübergreifendes Rollen-/Supportmodell

## 8. Priorisierte Backlog-Tasks (sofort umsetzbar)
1. **Architektur-ADR 001** (Monolith + modulare Schichten)
2. **Datenmodell v0** inkl. ER-Diagramm
3. **Migrations-Setup** (Alembic initial)
4. **Auth + Rollen v0**
5. **Kontenrahmen-Importer SKR03**
6. **JournalEntry Use-Case** mit starker Validierung
7. **AuditLog Middleware**
8. **Summen-/Saldenliste Report**
9. **Bilanz/GuV MVP**
10. **Testdaten-Generator für Demo-Mandanten**

## 9. Vorschlag Team-/Rollenaufteilung
- Product/Accounting Lead: HGB-Fachlichkeit, Abnahme Reports
- Backend Lead: Domänenmodell, Buchungslogik, Integrität
- Frontend Engineer: Eingabemasken, Usability, Reporting-UI
- QA/Automation: Testpyramide, Regressionssuite, E2E
- DevOps/SRE (teilzeit): CI/CD, Backup, Monitoring

## 10. Definition of Done (DoD)
Ein Feature gilt erst als fertig, wenn:
- fachliche Akzeptanzkriterien erfüllt sind,
- Unit- und Integrationstests vorhanden sind,
- Auditierbarkeit sichergestellt ist,
- Dokumentation (User + Dev) aktualisiert ist,
- Migrationen und Rollback getestet sind.

## 11. Risiken & Gegenmaßnahmen
- **Rechtliche/fachliche Komplexität (HGB/Steuer):**
  - Gegenmaßnahme: frühzeitig Steuerberater-Beirat einbinden
- **Datenkonsistenz bei Korrekturen:**
  - Gegenmaßnahme: Storno-Prinzip technisch erzwingen
- **Scope Creep:**
  - Gegenmaßnahme: strikte MVP-Grenze und quartalsweise Re-Priorisierung
- **DB-Portabilität:**
  - Gegenmaßnahme: CI-Matrix mit SQLite + PostgreSQL

## 12. Nächster Schritt (direkt nach diesem Plan)
1. ADR 001 + Domänenmodell-Entwurf erstellen
2. 2-wöchigen Sprint für Phase 0 planen
3. Vertikalen Prototyp bauen:
   - Mandant anlegen
   - Konto anlegen
   - Buchung erfassen
   - Summen-/Saldenliste anzeigen
