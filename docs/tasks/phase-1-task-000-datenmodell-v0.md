# Task P1-000: Datenmodell v0 definieren

## Ziel
Ein belastbares Datenmodell für die Kernbuchhaltung als Grundlage für alle Phase-1-Features erstellen.

## Kontext
Dieser Task ist ein **Voraussetzungs-Task** für Import, Buchungslogik, Belegverknüpfung und Reporting.

## Scope
- Fachliche Entitäten und Relationen spezifizieren:
  - `Tenant`, `Company`, `FiscalYear`, `Period`, `PeriodLock`
  - `Account`, `TaxCode`
  - `JournalEntry`, `JournalEntryLine`
  - `Document`, `AuditLog`
- Pflichtfelder, Schlüssel und Constraints festlegen (inkl. Soll/Haben-Integritätsregeln auf Modellebene, soweit sinnvoll)
- SQLAlchemy-Modelle als v0 implementieren
- Initiale Alembic-Migration(en) erstellen
- Kurzes ER-Diagramm (Markdown oder Mermaid) ergänzen

## Akzeptanzkriterien
1. Kernentitäten sind als SQLAlchemy-Modelle vorhanden.
2. Relationen und FK-Constraints sind in Migrationen abgebildet.
3. Ein Review-Dokument (ER-Diagramm + Feldübersicht) liegt im Repo.
4. Tests prüfen mindestens:
   - Erstellung zentraler Entitäten
   - FK-Verknüpfungen
   - Basis-Integrität für Buchungszeilen

## Technische Hinweise
- Tenant-Fähigkeit von Anfang an berücksichtigen (`tenant_id` an relevanten Tabellen).
- DB-portabel bleiben (SQLite + PostgreSQL-kompatibles SQLAlchemy).
- Domänenlogik nicht im UI-Layer platzieren.

## Out of Scope
- Vollständige Business-Validierungslogik für alle Sonderfälle
- Reporting-Implementierung

## Definition of Done
- Modelle + Migrationen + Doku + Tests vorhanden
- CI läuft grün
- Nachfolgende Tasks (Importer/Buchungsmaske) können darauf aufbauen
