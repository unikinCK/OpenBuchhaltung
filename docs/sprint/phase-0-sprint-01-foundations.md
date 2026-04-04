# Sprintplan Phase 0 – Foundations (2 Wochen)

## Sprint-Metadaten
- **Sprint-ID:** P0-S1
- **Zeitraum:** 10 Arbeitstage (2 Wochen)
- **Bezug:** Task `P0-001` und Inputs aus `P0-000` (ADR-001 + Domänenmodell V0)
- **Sprintziel:** Der vertikale End-to-End-Basisflow (Mandant → Konto → Buchung → Summen-/Saldenliste) ist in einem reviewfähigen Prototyp implementiert, automatisiert getestet und über CI reproduzierbar lieferbar.

## Nicht-Ziele (Scope-Cut)
- Kein vollständiger Kontenrahmenimport (Phase 1).
- Keine finalen Reports über Summen-/Saldenliste hinaus (z. B. Bilanz, GuV).
- Keine produktive Dokumentenablage mit revisionssicherer Speicherung.
- Keine Mandantenübergreifende Benutzer-/Rechteverwaltung im Detail.

## Team-Setup und Kapazitätsannahme
- **Rollen:** Product/Accounting Lead, Backend Lead, Frontend Engineer, QA/Automation, DevOps/SRE (teilzeit).
- **Kapazität:** 1 Sprint mit Fokus auf Foundations-Abschluss statt Feature-Breite.
- **Planungsprinzip:** Kritischer Pfad zuerst (Domäne + Persistenz + Basis-UI), Qualitätssicherung ab Tag 1.

## Sprint-Backlog (priorisiert)

| Prio | ID | Workstream | Task | Owner | Aufwand | Abnahmekriterium |
|---|---|---|---|---|---|---|
| P1 | P0-S1-01 | Backend | SQLAlchemy-Modelle für MVP-Kernentitäten (`Tenant`, `Company`, `Account`, `JournalEntry`, `JournalEntryLine`) inkl. Relationen und Constraints implementieren. | Backend Lead | L | Migration läuft lokal/CI fehlerfrei; Kernentitäten sind per ORM erzeugbar und verknüpfbar. |
| P1 | P0-S1-02 | Backend | Domain-Service für JournalEntry-Validierung (Soll/Haben-Ausgleich, Mindestanzahl Lines, Statusregeln) umsetzen. | Backend Lead | M | Service blockiert ungültige Buchungen mit klaren Fehlermeldungen; Unit-Tests decken Hauptregeln ab. |
| P1 | P0-S1-03 | Backend | Tenant/Company-Scoping als wiederverwendbare Query-Policy im Datenzugriff integrieren. | Backend Lead | M | Alle relevanten Read-/Write-Pfade erzwingen Tenant-Scoping; Integrationstest weist Datenisolation nach. |
| P1 | P0-S1-04 | Frontend | Basis-UI für Anlage von Mandant und Konto bereitstellen (Formulare + serverseitige Validierungsausgabe). | Frontend Engineer | M | Anwender kann Mandant und Konto ohne manuelle DB-Eingriffe anlegen; Validierungsfehler sind sichtbar. |
| P1 | P0-S1-05 | Frontend/Backend | Buchungserfassungsflow (JournalEntry + Lines) inkl. Summenprüfung im Request-Handling implementieren. | Frontend Engineer + Backend Lead | L | Gültige Buchung wird gespeichert; ungültige Soll/Haben-Summen werden abgefangen und erklärt. |
| P1 | P0-S1-06 | Frontend | Summen-/Saldenliste als einfache tabellarische Ansicht aus persistierten Buchungen bereitstellen. | Frontend Engineer | M | Seite zeigt Kontostände nachvollziehbar pro Konto; Ergebnis ist mit Testdaten reproduzierbar. |
| P1 | P0-S1-07 | QA | Testpaket für vertikalen Basisflow aufsetzen (Unit + Integrations + mindestens 1 E2E Happy Path). | QA/Automation | M | CI führt Tests automatisch aus; ein roter Test verhindert Merge. |
| P2 | P0-S1-08 | DevOps | CI-Pipeline um Quality Gates erweitern (Tests, Lint, Migrationscheck). | DevOps/SRE | M | Pipeline-Status ist für alle PRs sichtbar; fehlschlagende Quality Gates blockieren Merge. |
| P2 | P0-S1-09 | Backend | Minimalen Document-Link-Placeholder am JournalEntry vorsehen (ohne produktive Storage-Integration). | Backend Lead | S | JournalEntry kann optional eine Dokument-Referenz halten; Verhalten ist dokumentiert und getestet. |
| P2 | P0-S1-10 | Product/QA | Review-Checkliste für fachliche Abnahme (Buchungslogik, Nachvollziehbarkeit, Scope-Grenzen) dokumentieren. | Product/Accounting Lead + QA | S | Checkliste liegt in `docs/` vor und wird im Sprint-Review angewendet. |

## Abhängigkeiten und Reihenfolge
1. **P0-S1-01** vor **P0-S1-02**, **P0-S1-03**, **P0-S1-05**, **P0-S1-09**
2. **P0-S1-02** und **P0-S1-03** vor finaler Umsetzung von **P0-S1-05**
3. **P0-S1-04** vor vollständigem Durchstich in **P0-S1-05**
4. **P0-S1-05** vor **P0-S1-06** und vor finalem E2E in **P0-S1-07**
5. **P0-S1-07** und **P0-S1-08** laufen früh parallel, müssen aber vor Sprint-Abnahme abgeschlossen sein

## Vorschlag Sprint-Taktung (Tag 1–10)
- **Tag 1–2:** Architektur-/Implementierungs-Kickoff, Start P0-S1-01/02/08
- **Tag 3–4:** Abschluss Persistenz-Basis, Scoping, Start UI-Grundlagen (P0-S1-04)
- **Tag 5–6:** Buchungsflow End-to-End (P0-S1-05), begleitende Tests
- **Tag 7:** Summen-/Saldenliste (P0-S1-06), E2E-Flow stabilisieren (P0-S1-07)
- **Tag 8:** Dokument-Placeholder (P0-S1-09), Bugfixing
- **Tag 9:** Fachliche Review-Checkliste und Abnahmelauf (P0-S1-10)
- **Tag 10:** Hardening, Restpunkte schließen, Sprint-Review + Retro

## Risiken, Mitigations und Eskalationspfad

| Risiko/Blocker | Eintrittswahrscheinlichkeit | Auswirkung | Mitigation | Eskalationspfad |
|---|---|---|---|---|
| Unklare Regel zu `JournalEntry`-Status (Draft vs. Posted) | Mittel | Rework in Validierungslogik | D-001 als Timebox-Entscheid in Woche 1, sonst konservativ auf minimalen MVP-Status einschränken | Backend Lead → Product/Accounting Lead → Architekturentscheid im Review |
| Scope Creep durch zusätzliche Reporting-Wünsche | Hoch | Sprintziel gefährdet | Harte Anwendung der Nicht-Ziele, neue Wünsche nur ins Backlog nach Sprint | Product/Accounting Lead → Sprint Owner |
| CI-Instabilität bei Migration/Test-Setup | Mittel | Verzögerte Integrationsfähigkeit | Frühzeitiges Aufsetzen von Quality Gates (P0-S1-08) + tägliche Pipeline-Kontrolle | DevOps/SRE → Backend Lead |
| Fachliche Validierung von Buchungslogik dauert länger | Mittel | Abnahme verzögert | Frühreview mit Beispieldatensätzen ab Sprintmitte | Product/Accounting Lead → Team-Review |

## Definition of Done pro Workstream
- **Backend**
  - Domain- und Persistenzlogik implementiert.
  - Relevante Unit-/Integrationstests grün.
  - Migrationen inkl. Rollback lokal verifiziert.
- **Frontend**
  - Formulare und Ergebnisansicht bedienbar.
  - Fehlerfälle sichtbar und verständlich.
  - Happy Path ohne manuelle Workarounds durchspielbar.
- **QA**
  - Testfälle dokumentiert und automatisiert ausführbar.
  - Mindestens ein E2E-Happy-Path im CI-Lauf enthalten.
- **DevOps**
  - CI läuft reproduzierbar auf Pull Requests.
  - Test/Lint/Migration als verpflichtende Gates aktiv.

## Übergabe in Issue-Tracking
Jeder Backlog-Eintrag wird als Issue mit folgenden Pflichtfeldern übernommen:
1. `ID` (z. B. P0-S1-05)
2. Kurzbeschreibung (Outcome-orientiert)
3. Owner
4. Aufwand (S/M/L)
5. Abnahmekriterium (kopiert aus Sprint-Backlog)
6. Verknüpfte Abhängigkeiten
