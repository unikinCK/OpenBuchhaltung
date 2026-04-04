# ADR-001: Monolith mit modularen Schichten

- **Status:** Angenommen
- **Datum:** 2026-03-28
- **Zuletzt aktualisiert:** 2026-04-04
- **Entscheider:** OpenBuchhaltung Kernteam
- **Verknüpfter Domänenentwurf:** `docs/review/datenmodell-v0.md`

## Kontext
Zum Projektstart müssen wir schnell lieferfähig sein, gleichzeitig aber die fachliche Komplexität
(Buchungslogik, Auditierbarkeit, Reporting) sauber trennen. Das Repository ist bereits entlang
`app/` und `domain/` strukturiert, und der nächste Umsetzungsschritt benötigt eine stabile,
reviewbare Architekturgrundlage.

## Entscheidung
Wir starten als **modularer Monolith auf Flask-Basis** mit klar getrennten Schichten und expliziten
Abhängigkeitsregeln:

1. **Presentation Layer (`app/`)**
   - Flask Blueprints, HTML-Templates, API-Endpunkte
   - Keine direkte Persistenzlogik in Views/Routes
2. **Application Layer (Use-Case-Services, neu aufzubauen)**
   - Orchestriert Anwendungsfälle (z. B. Buchung erfassen)
   - Verwaltet Transaktionsgrenzen und Berechtigungsprüfungen
3. **Domain Layer (`domain/`)**
   - Fachliche Entitäten, Value Objects, Invarianten
   - Keine Abhängigkeit auf Flask oder konkrete Datenbanktechnologie
4. **Persistence/Infrastructure Layer (`migrations/`, SQLAlchemy-Adapter)**
   - ORM-Mappings, Repositories, externe Integrationen

### Abhängigkeitsrichtung (strict)
`Presentation -> Application -> Domain <- Persistence`

Die Domain darf nicht von äußeren Schichten abhängen. Persistenzdetails werden über Adapter/Repository-
Schnittstellen an die Application/Domain angebunden.

## Entscheidungsbegründung
- **Liefergeschwindigkeit:** Ein Monolith reduziert initiale Betriebs- und Integrationskomplexität.
- **Fachliche Klarheit:** Schichtgrenzen verhindern, dass Buchhaltungsregeln in UI- oder DB-Code „auslaufen“.
- **Migrationspfad:** Modulare Trennung ermöglicht später eine kontrollierte Extraktion einzelner Module,
  falls Last, Teamgröße oder Deployment-Topologie dies erfordern.
- **Testbarkeit:** Use-Cases und Domainregeln können unabhängig von Flask/DB getestet werden.

## Konsequenzen
### Positiv
- Schneller Start ohne verteilte Systemkomplexität.
- Konsistente Struktur für Phase 0/1 und klare Review-Kriterien.
- Verbesserte Wartbarkeit durch explizite Schichtverantwortung.

### Negativ
- Skalierung zunächst primär vertikal.
- Teamdisziplin und Review-Gates nötig, damit Schichtgrenzen nicht erodieren.
- Zusätzlicher Initialaufwand für Application-Layer-Schnittstellen.

### Verbindliche Leitplanken
- Kein SQLAlchemy-Session-Zugriff aus `app/`-Routen.
- Domainvalidierungen (z. B. Soll/Haben-Ausgleich) liegen im Domain/Application-Layer.
- Tenant-Isolation wird als querschnittliche Invariante in allen Schichten erzwungen.

## Alternativen
- **Microservices ab Start:** verworfen wegen hoher operativer Komplexität.
- **Klassischer Schichten-Monolith ohne Modulgrenzen:** verworfen wegen Wartbarkeitsrisiko.
- **Modularer Monolith ohne expliziten Application-Layer:** verworfen, da Use-Case-Logik sonst in
  Presentation/Persistence ausfranst.

## Entscheidungsbedarf (Folgeentscheidungen)
1. **A-001:** Konkrete Modulgrenzen innerhalb der Domain (z. B. Accounting, Reporting, Tax, Documents)
   inkl. Ownership bis Ende Phase 1 festlegen.
2. **A-002:** Repository-/Unit-of-Work-Pattern verbindlich entscheiden (inkl. Fehler- und
   Transaktionsstrategie für Flask-Requests).
3. **A-003:** Tenant-Kontext-Durchreichung standardisieren (Middleware vs. explizite Parameter),
   inklusive Teststrategie gegen Tenant-Datenlecks.
4. **A-004:** Ereignis-/Outbox-Strategie vorbereiten, falls asynchrone Workflows (OCR/Exports) ab Phase 1
   wachsen.
