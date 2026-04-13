# Sprint B – Kernbuchhaltung MVP (Detailplan)

## Sprint-Metadaten
- **Sprint-ID:** P1-SB
- **Dauer:** 10 Arbeitstage
- **Zeitraum:** 2026-04-13 bis 2026-04-24
- **Sprintziel:** Nutzbarkeit und Compliance des Kernflows erhöhen: Exportierbarkeit, Rechte-Härtung, Abschluss der zentralen MVP-Berichte.

## Scope (Sprint B)
1. **P1-004 CSV-Export Core**
   - CSV-Export für Summen-/Saldenliste aus UI bereitstellen.
   - Download mit stabiler Dateinamenskonvention ermöglichen.
2. **P1-005 Autorisierung und Audit-Abdeckung**
   - Rollenprüfung für kritische Schreiboperationen ergänzen.
   - Audit-Events für Dokument-Upload und Export ergänzen.
3. **P1-003 GuV/Bilanz Report MVP**
   - MVP-Report aus Kontenklassenlogik bereitstellen.
4. **P1-006 E2E Kernflows**
   - End-to-End-Test: Buchung + Beleg + Export.

## Nicht-Ziele
- DATEV-Export in voller Spezifikation.
- OCR- und Belegklassifikationsautomatisierung.
- Vollständige Rollenmatrix über alle zukünftigen Module.

## Detaillierter Umsetzungsplan

### Track 1: CSV-Export (P1-004)
- [x] Export-Route für Summen-/Saldenliste ergänzen.
- [x] CSV-Inhalt mit Spalten `Konto, Name, Soll, Haben, Saldo` erzeugen.
- [x] Dateiname gemäß Schema `susa-<company_id>-<date>.csv` zurückgeben.
- [x] Integrationstest für CSV-Download ergänzen.

### Track 2: Rechte + Audit (P1-005)
- [ ] Schreiboperationen (Konto, Buchung, Belegupload) rollenbasiert absichern.
- [ ] Audit-Log auf Upload + Export erweitern.
- [ ] Negative Tests (403 bei fehlender Rolle) ergänzen.

### Track 3: GuV/Bilanz MVP (P1-003)
- [ ] GuV-Sicht aus revenue/expense Aggregation bereitstellen.
- [ ] Bilanzsicht aus asset/liability/equity Aggregation bereitstellen.
- [ ] API + UI-Integration sowie Basistests ergänzen.

### Track 4: E2E Kernflow (P1-006)
- [ ] E2E-Flow (Anlage Stammdaten → Buchung → Belegupload → CSV-Export) automatisieren.

## Ausführung in diesem Changeset
In diesem Changeset wird **Track 1 (P1-004 CSV-Export Core)** vollständig umgesetzt. Die Tracks 2–4 bleiben als nächste Sprint-B-Inkremente im Backlog.
