# Task P1-006: End-to-End-Testpaket für Kernflows

## Ziel
Ein robustes E2E-Testpaket für die MVP-Hauptprozesse bereitstellen, um Regressionen früh zu erkennen.

## Kontext
Es existieren bereits viele Integrations-/API-Tests. Fehlend ist ein konsolidierter Ende-zu-Ende-Nutzerfluss über mehrere Module.

## Abhängigkeit
- **Input aus:** P1-002 bis P1-005 (mindestens teilweise umgesetzt)

## Scope
- E2E-Szenario: Mandant anlegen → Konten importieren → Buchung erfassen → Beleg verknüpfen → Report/Export prüfen
- Positiv- und Negativpfade (z. B. gesperrte Periode, fehlende Rechte)
- CI-Integration als verpflichtendes Quality Gate
- Testdatenstrategie für reproduzierbare Läufe

## Akzeptanzkriterien
1. Mindestens ein vollständiger Happy-Path läuft automatisiert durch.
2. Mindestens zwei fachliche Negativfälle sind als E2E abgedeckt.
3. E2E-Lauf ist in CI eingebunden und dokumentiert.
4. Flaky-Rate ist niedrig/nachvollziehbar (Retries oder Stabilitätsmaßnahmen dokumentiert).

## Technische Hinweise
- Bestehende Testfixtures wiederverwenden.
- Für UI-E2E Headless-Strategie und Artefakt-Upload (Logs/Screenshots) definieren.

## Out of Scope
- Last-/Performance-Tests
- Cross-Browser-Matrix

## Definition of Done
- E2E-Suite im Repo + CI aktiv
- Ausführung lokal dokumentiert
- Qualitätsgate wirksam
