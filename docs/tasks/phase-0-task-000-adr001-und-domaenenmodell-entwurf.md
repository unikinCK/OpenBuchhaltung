# Task P0-000: ADR 001 + Domänenmodell-Entwurf erstellen

## Ziel
Die Architekturentscheidung für den Start festhalten und einen belastbaren Domänenmodell-Entwurf als Grundlage für Umsetzung und Sprintplanung bereitstellen.

## Kontext
Dieser Task setzt den im Umsetzungsplan definierten „nächsten Schritt“ um und schafft die fachliche sowie technische Basis für den vertikalen Prototyp.

## Scope
- ADR-001 prüfen, schärfen und finalisieren (Monolith + modulare Schichten)
- Domänenentitäten für den MVP-Start beschreiben:
  - `Tenant`, `Company`, `Account`, `JournalEntry`, `JournalEntryLine`, `TaxCode`, `Document`
- Kernrelationen und Integritätsregeln dokumentieren
- Offene Architekturfragen als „Entscheidungsbedarf“ im Dokument markieren
- Verlinkung zwischen ADR und Domänenentwurf ergänzen

## Akzeptanzkriterien
1. ADR-001 ist fachlich/technisch reviewbar und enthält klare Entscheidungsbegründungen.
2. Ein Domänenmodell-Entwurf mit Entitäten, Relationen und Kernregeln liegt im Repo vor.
3. Mindestens 3 offene Punkte sind als explizite Folgeentscheidungen dokumentiert (falls noch ungeklärt).
4. Der Entwurf ist so konkret, dass darauf Sprint-Aufgaben heruntergebrochen werden können.

## Technische Hinweise
- Struktur an den vorhandenen Docs orientieren (`docs/adr/`, `docs/review/`).
- Konsistente Benennung mit bestehendem Domain-Layer verwenden.
- Fokus auf MVP-Relevanz; keine Vorab-Optimierung für V2.

## Out of Scope
- Vollständige Implementierung der SQLAlchemy-Modelle
- Produktive Migrationsumsetzung

## Definition of Done
- ADR + Domänenentwurf im Repository aktualisiert/ergänzt
- Reviewfähig und für Sprint-Planung nutzbar
