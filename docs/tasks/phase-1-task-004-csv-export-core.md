# Task P1-004: CSV-Export Core (Journal + Summen/Salden)

## Ziel
Exportierbare CSV-Dateien für zentrale MVP-Daten bereitstellen.

## Kontext
Export ist ein MVP-Anforderungspunkt und unterstützt externe Prüfung/Weiterverarbeitung.

## Abhängigkeit
- **Input aus:** JournalEntry- und Trial-Balance-Funktionen

## Scope
- CSV-Export für Journalbuchungen (Kopf + Zeilen)
- CSV-Export für Summen-/Saldenliste
- API-Endpunkte oder CLI-Befehle für Export
- Einheitliches Datums-/Dezimalformat und UTF-8-Encoding
- Downloadfähiger UI-Trigger (minimal)

## Akzeptanzkriterien
1. Beide Exporte erzeugen valide CSV-Dateien mit Header.
2. Export kann auf Gesellschaftsebene gefiltert werden.
3. Format ist konsistent (Trennzeichen, Encoding, Dezimaldarstellung).
4. Tests verifizieren Header, Zeilenanzahl und Beispielwerte.

## Technische Hinweise
- Für große Datenmengen Streaming vorbereiten (mindestens architekturell).
- Dateinamenkonvention dokumentieren (`<report>-<company>-<date>.csv`).

## Out of Scope
- DATEV-Export
- XLSX/PDF-Export

## Definition of Done
- Exportfunktion produktiv nutzbar
- API/CLI dokumentiert
- Tests grün
