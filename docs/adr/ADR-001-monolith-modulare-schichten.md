# ADR-001: Monolith mit modularen Schichten

- **Status:** Angenommen
- **Datum:** 2026-03-28
- **Entscheider:** OpenBuchhaltung Kernteam

## Kontext
Zum Projektstart müssen wir schnell lieferfähig sein, gleichzeitig aber die fachliche Komplexität
(Buchungslogik, Auditierbarkeit, Reporting) sauber trennen.

## Entscheidung
Wir starten als modularer Monolith auf Flask-Basis mit klar getrennten Schichten:
Presentation (`app/`), Domain (`domain/`) und später Persistence/Infrastructure.

## Konsequenzen
- Positiv:
  - Schneller Start ohne verteilte Systemkomplexität.
  - Klare Entwicklungsstruktur für spätere Extraktion einzelner Module.
- Negativ:
  - Skalierung ist zunächst vertikal begrenzt.
  - Teamdisziplin erforderlich, um Schichtgrenzen einzuhalten.
- Offene Punkte:
  - Exakte Modulgrenzen je Fachbereich in Phase 1 ausarbeiten.

## Alternativen
- **Microservices ab Start:** verworfen wegen hoher operativer Komplexität.
- **Klassischer Schichten-Monolith ohne Modulgrenzen:** verworfen wegen Wartbarkeitsrisiko.
