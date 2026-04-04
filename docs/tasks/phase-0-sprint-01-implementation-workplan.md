# Phase 0 Sprint 1 – Umsetzungsplan in Code-Tasks

## Ziel
Den Sprint-Backlog aus `docs/sprint/phase-0-sprint-01-foundations.md` in eine konkrete, umsetzbare
Reihenfolge von Entwicklungs-Tasks zerlegen, mit klaren Test-Stopps für manuelle Fachvalidierung.

## Task-Zerlegung (Coding)

1. **P0-S1-02A: JournalEntry-Validierungsservice**
   - Domain-Service mit Regeln für Status, Mindestanzahl Zeilen und Soll/Haben-Ausgleich.
   - Unit-Tests für Happy Path und Hauptfehlerfälle.
   - **Status:** umgesetzt.

2. **P0-S1-04A: Basis-UI Mandant/Gesellschaft anlegen**
   - Formular und POST-Handler mit serverseitiger Validierung und Flash-Feedback.
   - Persistenz über SQLAlchemy-Session.
   - **Status:** umgesetzt.

3. **P0-S1-04B: Basis-UI Konto anlegen**
   - Kontoformular mit Gesellschaftsauswahl.
   - Persistente Anlage eines Kontos inklusive tenant/company-Referenzen.
   - **Status:** umgesetzt.

4. **P0-S1-07A: Integrationstests Basisflow**
   - HTTP-Tests für Anlage von Mandant/Gesellschaft und Konto.
   - Unit-Tests für JournalEntry-Validierung.
   - **Status:** umgesetzt.

5. **P0-S1-03A: Tenant/Company-Scoping als Query-Policy**
   - Generische Scoping-Hilfsfunktionen im Datenzugriff.
   - Integrationstests für Datenisolation.
   - **Status:** umgesetzt.

6. **P0-S1-05A: Buchungserfassungsflow (JournalEntry + Lines)**
   - Eingabeformular + Request-Mapping auf Domain-Validierung.
   - Persistenz inkl. aussagekräftiger Fehlermeldungen.
   - **Status:** umgesetzt.

7. **P0-S1-06A: Summen-/Saldenliste**
   - Aggregationsquery je Konto.
   - Einfache tabellarische Ansicht mit reproduzierbaren Testdaten.
   - **Status:** umgesetzt.

## Review-Checkpoint
Die End-to-End-Kette ist jetzt im Prototyp vorhanden:
1. Mandant + Gesellschaft anlegen
2. Konto anlegen
3. Buchung erfassen
4. Summen-/Saldenliste prüfen

Nächster Schwerpunkt: Audit-Log und Periodensperren in den Buchungsflow integrieren.
