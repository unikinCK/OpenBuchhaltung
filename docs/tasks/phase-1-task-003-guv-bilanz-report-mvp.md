# Task P1-003: GuV/Bilanz-Report (HGB-Basisschema) MVP

## Ziel
Einen ersten GuV- und Bilanz-Report für eine Gesellschaft und Periode bereitstellen.

## Kontext
Aktuell ist die Summen-/Saldenliste vorhanden. Für MVP-Fachnutzen fehlen GuV/Bilanz als Kernauswertungen.

## Abhängigkeit
- **Input aus:** Trial-Balance-Logik
- **Input aus:** Kontentyp-/Kontenklassifikation

## Scope
- Mapping von Konten auf GuV/Bilanz-Positionen (MVP-Regelsatz)
- Report-Service für GuV/Bilanz
- API-Endpunkte für beide Reports
- Basis-UI-Ansicht inkl. Periodenfilter
- Dokumentation der Zuordnungslogik inkl. bekannter Grenzen

## Akzeptanzkriterien
1. GuV und Bilanz sind für eine Gesellschaft abrufbar.
2. Summenbildung ist reproduzierbar und testbar.
3. Bilanzgleichheit wird geprüft und sichtbar gemacht.
4. Mindestens ein Golden-Path-Testfall mit erwarteten Reportwerten ist vorhanden.

## Technische Hinweise
- Mapping deklarativ halten (Konfiguration statt harter if/else-Logik).
- Fehlerhafte oder unvollständige Kontenzuordnung explizit ausweisen.

## Out of Scope
- Vollständige HGB-Feingliederung für alle Sonderfälle
- Anhänge/Lagebericht

## Definition of Done
- GuV/Bilanz via API/UI verfügbar
- Mapping dokumentiert
- Report-Tests grün
