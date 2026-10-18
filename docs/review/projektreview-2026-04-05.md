# Projekt-Review – Stand 2026-04-05

## Ziel des Reviews
Dieses Review konsolidiert den aktuellen Umsetzungsstand auf Basis von Code, Tests und bestehender Planung.
Es beantwortet:
1. Welche Aufgaben sind abgeschlossen?
2. Welche Themen stehen als Nächstes an?
3. Welche konkreten Tasks sollten dafür ins Backlog?

## Bewertungsbasis
- Planungsstand aus `docs/umsetzungsplan.md`
- Umgesetzte Features in API/UI/Services
- Abgedeckte Szenarien in den automatisierten Tests

---

## 1) Abgeschlossene Aufgaben

### Foundations (Phase 0)
**Status: abgeschlossen.**

Begründung:
- Vertikaler Basisflow ist vollständig im UI vorhanden (Mandant, Konto, Buchung, SuSa). 
- Sprint-Workplan dokumentiert alle P0-S1 Coding-Tasks als „umgesetzt“.

### Kernbuchhaltung – bereits erledigte/weitgehend erledigte Punkte aus Phase 1

1. **Kontenrahmenimport SKR03/SKR04: abgeschlossen**
   - CLI-Import vorhanden, inklusive idempotentem Verhalten und Fehlerhandling.

2. **Buchungsmaske (Soll/Haben): abgeschlossen (MVP-Level)**
   - UI unterstützt mehrzeilige Buchungen, API unterstützt strukturierte Buchungszeilen.

3. **Validierungsregeln: weitgehend abgeschlossen**
   - Soll/Haben-Ausgleich, Zeilenregeln, Periodensperren und Konten-Prüfungen sind implementiert.

4. **Audit-Log für buchungsrelevante Aktionen: abgeschlossen für JournalEntry-Erfassung**
   - Beim Buchen wird ein Audit-Event geschrieben.

5. **Summen-/Saldenliste: abgeschlossen**
   - Report-Service + API + UI vorhanden.

---

## 2) Offene Themen / Nächste Arbeitspakete

1. **Belegupload + Verknüpfung mit Buchungen**
   - Im Datenmodell ist `Document` vorhanden, aber Upload-/Storage-/Verknüpfungsflow fehlt noch.

2. **GuV/Bilanz-Report (HGB-Basisschema)**
   - Bisher ist nur die Summen-/Saldenliste produktiv.

3. **CSV-Export**
   - Planseitig gefordert, aber noch nicht als Feature-Flow umgesetzt.

4. **Rollen-/Rechte-Härtung in Kernflows**
   - Aktuell Demo-Login/Session-Basis; feinere Autorisierung für API/UI fehlt.

5. **End-to-End-Testabdeckung über vollständigen Kernprozess**
   - Gute Integrations-/API-Tests sind da, aber ein expliziter Endnutzer-E2E-Flow (inkl. Dokument + Export) sollte ergänzt werden.

---

## 3) Neu angelegte Tasks (Backlog)

Für die offenen Themen wurden folgende Tasks erstellt:

1. `docs/tasks/phase-1-task-002-belegupload-und-verknuepfung.md`
2. `docs/tasks/phase-1-task-003-guv-bilanz-report-mvp.md`
3. `docs/tasks/phase-1-task-004-csv-export-core.md`
4. `docs/tasks/phase-1-task-005-autorisierung-und-audit-abdeckung.md`
5. `docs/tasks/phase-1-task-006-e2e-kernflows.md`

---

## 4) Priorisierungsvorschlag

### Jetzt (nächster Sprint)
1. P1-002 Belegupload und Verknüpfung
2. P1-004 CSV-Export Core
3. P1-005 Autorisierung & Audit-Abdeckung

### Danach
4. P1-003 GuV/Bilanz-Report MVP
5. P1-006 E2E-Kernflows

Begründung: Belegfluss, Export und Rechte wirken direkt auf Nutzbarkeit, Nachvollziehbarkeit und Compliance im MVP.
