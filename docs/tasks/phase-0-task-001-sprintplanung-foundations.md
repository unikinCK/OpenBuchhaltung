# Task P0-001: 2-wöchigen Sprint für Phase 0 planen

## Ziel
Einen umsetzbaren Sprintplan (2 Wochen) für den Abschluss der Foundations-Phase mit klaren Arbeitspaketen, Verantwortlichkeiten und Abnahmekriterien erstellen.

## Kontext
Nach ADR- und Domänenentwurf wird ein konkreter Delivery-Plan benötigt, um den vertikalen Prototypen organisiert umzusetzen.

## Abhängigkeit
- **Input aus:** `P0-000 ADR 001 + Domänenmodell-Entwurf`

## Scope
- Sprintziel und Nicht-Ziele definieren
- Stories/Tasks in umsetzbare Pakete schneiden (Backend, Frontend, QA, DevOps)
- Abhängigkeiten und Reihenfolge festlegen
- Aufwand grob schätzen (z. B. S/M/L oder Story Points)
- Risiken und Mitigations für den Sprint dokumentieren
- Definition of Done pro Workstream konkretisieren

## Akzeptanzkriterien
1. Ein Sprint-Backlog mit priorisierten Tasks liegt dokumentiert vor.
2. Für jeden Task sind Owner, geschätzter Aufwand und Abnahmekriterium angegeben.
3. Es gibt ein explizites Sprintziel und einen klaren Scope-Cut für „nicht im Sprint“.
4. Risiken/Blocker und Eskalationspfad sind dokumentiert.

## Technische Hinweise
- Sprintplanung in `docs/` versioniert ablegen (Markdown).
- Tasks so formulieren, dass sie direkt in Issues überführt werden können.
- QA-Aufgaben (Tests/Lint/Migrationscheck) von Anfang an einplanen.

## Out of Scope
- Detaillierte Quartalsroadmap
- Vollständige Ressourcenplanung über den Sprint hinaus

## Definition of Done
- Sprintplan ist im Repo vorhanden
- Team kann auf Basis des Plans mit Umsetzung starten
