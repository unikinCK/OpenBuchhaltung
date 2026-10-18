# Sprint A Detailplan (2026-04-13)

## Sprint-Ziel
In Sprint A werden die offenen Phase-1-MVP-Punkte **CSV-Export Core** und **GuV/Bilanz-Report MVP** produktiv über API und Basis-UI ausgeliefert.

## Scope
1. **P1-004 CSV-Export Core**
   - API-Export für Journal (`/api/v1/exports/journal.csv`).
   - API-Export für Summen-/Saldenliste (`/api/v1/exports/trial-balance.csv`).
   - Minimaler UI-Trigger (Download-Links auf der Startseite).
2. **P1-003 GuV/Bilanz MVP**
   - GuV-Service und API-Endpunkt (`/api/v1/income-statement`).
   - Bilanz-Service und API-Endpunkt (`/api/v1/balance-sheet`) inkl. Bilanzgleichheitsindikator.
   - Basisdarstellung der Totals in der UI.

## Arbeitspakete (inkl. Reihenfolge)
1. Reporting-Services erweitern (Kontosalden je Kontotyp, GuV, Bilanz).
2. API-Endpunkte für GuV/Bilanz bereitstellen.
3. CSV-Exports für Journal und Summen-/Saldenliste bereitstellen.
4. UI um GuV-/Bilanz-Totals und CSV-Export-Links ergänzen.
5. Integrationstests für neue Endpunkte und Export-Header implementieren.
6. Lint + Tests ausführen.

## Akzeptanzkriterien Sprint A
- GuV und Bilanz sind für `company_id` per API abrufbar.
- Bilanzantwort enthält `is_balanced` und `difference`.
- CSV-Exports liefern gültigen CSV-Inhalt mit Header und `text/csv` Content-Type.
- UI bietet Download-Links für beide CSV-Exporte sowie GuV/Bilanz-Totals.
- Tests decken mindestens einen Golden Path für GuV/Bilanz und CSV-Export ab.

## Ergebnis
- **Status:** umgesetzt.
- Offene Restpunkte für nächste Sprints: Autorisierung/Audit-Härtung, E2E-Kernflows, LLM-Integration für Belegupdates.
