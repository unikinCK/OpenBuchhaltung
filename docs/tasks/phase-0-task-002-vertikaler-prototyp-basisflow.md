# Task P0-002: Vertikalen Prototyp (Basisflow) umsetzen

## Ziel
Einen durchgängigen MVP-Basisflow bereitstellen: Mandant anlegen, Konto anlegen, Buchung erfassen und Summen-/Saldenliste anzeigen.

## Kontext
Dieser Task validiert die End-to-End-Kernkette der Anwendung frühzeitig und reduziert Integrationsrisiken vor Phase 1.

## Abhängigkeit
- **Input aus:** `P0-000` (Architektur + Domänenentwurf)
- **Input aus:** `P0-001` (Sprintplanung)

## Scope
- UI/API-Flow zum Anlegen eines Mandanten
- UI/API-Flow zum Anlegen eines Kontos
- UI/API-Flow zur Erfassung einer einfachen Journalbuchung (Soll/Haben)
- Anzeige einer einfachen Summen-/Saldenliste
- Mindestens ein Integrationstest für den End-to-End-Flow
- Seed-/Demo-Daten für manuelle Prüfung

## Akzeptanzkriterien
1. Ein neuer Mandant kann erstellt und persistiert werden.
2. Für den Mandanten kann mindestens ein Konto angelegt werden.
3. Eine Buchung mit ausgeglichenen Soll/Haben-Werten kann gespeichert werden.
4. Die Summen-/Saldenliste zeigt den Buchungseffekt korrekt an.
5. Ein automatisierter Test deckt den Basisflow ab.

## Technische Hinweise
- Tenant-Filterung in allen Schritten strikt einhalten.
- Buchungsvalidierung früh integrieren (keine unausgeglichenen Buchungen).
- Reporting zunächst minimal halten (Korrektheit > Design).

## Out of Scope
- Vollständige Steuerlogik/USt-Sonderfälle
- Komplexes Rollen-/Rechte-Feintuning
- DATEV-Export

## Definition of Done
- Basisflow manuell und per Test nachvollziehbar
- Dokumentation für Ausführung/Validierung ergänzt
- Grundlage für Phase-1-Features ist vorhanden
