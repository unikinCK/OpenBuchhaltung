# Coding Guidelines (Phase 0)

## Architektur
- Nutze Application Factory Pattern für Flask (`create_app`).
- Trenne Schichten in `app/` (Presentation), `domain/` (Fachlogik), später `infrastructure/`.
- Halte Controller dünn, verschiebe Regeln in Use-Cases/Domain-Services.
- **Schnittstellen-Parität:** Jede Fachfunktion wird über alle drei Schnittstellen
  angeboten und gepflegt — UI (`app/web/`), REST-API (`app/api/`) und MCP-Server
  (`app/services/mcp_server.py`, ToolSpec-Proxy auf die API). Neue Features liefern
  alle drei Schichten mit; vor dem Merge Parität prüfen.

## Python-Stil
- Python 3.12 als Zielversion.
- Linting über `ruff`.
- Maximal 100 Zeichen pro Zeile.
- Bevorzuge klare Funktionsnamen und kurze Funktionen.

## Tests
- Neue Features mindestens mit einem Unit- oder Integrationstest absichern.
- Fehlerpfade explizit testen (z. B. Login mit ungültigen Daten).

## Security-Basics
- Keine Secrets im Repository.
- Session/Authentifizierung nur mit gehashten Passwörtern in produktionsnahen Umgebungen.
- Zugriffsrechte zentral prüfen, keine verstreuten Rollenchecks.

## Dokumentation
- Architekturentscheidungen als ADR erfassen (`docs/adr`).
- Jede größere Änderung im Umsetzungsplan oder in technischen Notizen reflektieren.
