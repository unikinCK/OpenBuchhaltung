# Task P1-001: Kontenrahmenimport (SKR03/SKR04)

## Ziel
Import von SKR03- und SKR04-Konten als Grundlage für die Buchungsmaske in Phase 1.

## Kontext
Dieser Task adressiert den ersten fachlichen Feature-Block aus **Phase 1 – Kernbuchhaltung MVP**,
setzt aber ein vorhandenes Datenmodell voraus.

## Abhängigkeit
- **Blockiert durch:** `P1-000 Datenmodell v0 definieren`

## Scope
- CSV-Import für SKR03 (MVP) und SKR04 (optional im gleichen Task oder direkt danach)
- Persistierung in `Account`-Tabelle
- Duplikat-Handling (idempotenter Import)
- Validierung der Pflichtfelder (Kontonummer, Bezeichnung, Kontoart)
- Basis-CLI oder Admin-Endpoint zum Auslösen des Imports

## Akzeptanzkriterien
1. Ein SKR03-CSV kann vollständig importiert werden.
2. Der erneute Import derselben Datei erzeugt keine doppelten Konten.
3. Fehlerhafte Zeilen werden protokolliert und blockieren nicht den gesamten Import.
4. Ein Test deckt mindestens einen erfolgreichen Import und einen Duplikat-Fall ab.

## Technische Hinweise
- Import-Logik in Application/Domain-naher Schicht kapseln (nicht direkt im Template/View).
- Persistenzzugriffe über SQLAlchemy.
- Keine Implementierung starten, bevor `Account`-Modell und Migration aus P1-000 gemerged sind.

## Out of Scope
- Vollständige Kontenpflege-UI
- Erweiterte SKR-Mappings jenseits der MVP-Pflichtfelder

## Definition of Done
- Akzeptanzkriterien erfüllt
- Unit-/Integrationstests grün
- Kurze Entwicklerdoku zur Import-Nutzung ergänzt
