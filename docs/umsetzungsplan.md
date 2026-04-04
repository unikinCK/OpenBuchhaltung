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
- [ ] Buchungsmaske (Soll/Haben, Steuercode, Beleglink)
- [ ] Validierungsregeln (Bilanzgleichheit, gesperrte Perioden)
- [ ] Belegupload + Speicherung + Verknüpfung
- [ ] Audit-Log für alle buchungsrelevanten Aktionen
- [ ] Summen-/Saldenliste
- [ ] GuV/Bilanz-Report (HGB-Basisschema)
- [ ] CSV-Export
- [ ] End-to-End-Tests für Kernflows

## Phase 2 – Prozesse & Qualität (4–6 Wochen)
- [ ] Jahresabschluss-Workflow (Periodenabschluss, Vortrag)
- [ ] OPOS-Basis Debitor/Kreditor
- [ ] Bank-CSV-Import + Matching-Regeln
- [ ] Performance-Profiling großer Journaldaten
- [ ] Security-Hardening + PenTest-Light

## Phase 3 – Ökosystem & Automatisierung (6–12 Wochen)
- [ ] DATEV-kompatiblere Exporte ausbauen
- [ ] E-Rechnung Import/Export
- [ ] OCR-Pipeline für Belege
- [ ] REST-API + API-Tokens
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
