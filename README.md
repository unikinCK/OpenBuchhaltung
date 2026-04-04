# OpenBuchhaltung
Webbasierte Open Source Buchhaltungssoftware.

## Planung
- Umsetzungsplan: `docs/umsetzungsplan.md`

## Phase 0 Setup
1. Virtuelle Umgebung erstellen und aktivieren
2. Abhängigkeiten installieren
   ```bash
   pip install -r requirements-dev.txt
   ```
3. Tests und Linting ausführen
   ```bash
   ruff check .
   pytest
   ```
4. Anwendung starten
   ```bash
   python run.py
   ```

Optional mit Containern:
```bash
docker compose up --build
```

## REST API (API-First)

Basis-Endpunkte:

- `GET /api/v1/health`
- `POST /api/v1/tenants` (legt Mandant + Gesellschaft an)
- `GET /api/v1/companies`
- `POST /api/v1/accounts`

Beispiel:
```bash
curl -X POST http://localhost:5000/api/v1/tenants \
  -H "Content-Type: application/json" \
  -d '{"tenant_name":"Mandant A","company_name":"Mandant A GmbH","currency_code":"EUR"}'
```


## UI-Screenshot Tool

Für schnelle UI-Checks gibt es ein Screenshot-Skript:

```bash
python tools/screenshot_ui.py --url http://127.0.0.1:5000/ --output artifacts/ui-home.png
```

Einmalig Browser-Binaries installieren:

```bash
python -m playwright install chromium
```

## LLM/MCP Kommunikation

Es gibt einen MCP-Bridge-Endpunkt:

- `POST /api/v1/mcp/call`

Die App leitet JSON-RPC-Aufrufe an einen konfigurierten MCP-Server weiter.

Konfiguration:
```bash
export MCP_SERVER_URL="http://localhost:8080/mcp"
```

Beispiel:
```bash
curl -X POST http://localhost:5000/api/v1/mcp/call \
  -H "Content-Type: application/json" \
  -d '{"id":"1","method":"tools/list","params":{}}'
```

## Kontenrahmenimport (SKR03/SKR04)

CSV-Import per Flask-CLI (idempotent, Duplikate werden uebersprungen).

Vorgebundene Kontenrahmen aus dem Repo importieren:

```bash
flask --app run.py import-kontenrahmen --company-id 1 --chart skr03
flask --app run.py import-kontenrahmen --company-id 1 --chart skr04
```

Alternativ eigene CSV-Datei importieren:

```bash
flask --app run.py import-kontenrahmen --company-id 1 --csv-path ./mein_kontenrahmen.csv
```

Hinweis: Genau eine Quelle muss angegeben werden (`--chart` oder `--csv-path`).

Unterstuetzte Kopfzeilen (Alias):
- `code` oder `Kontonummer`
- `name` oder `Bezeichnung`
- `account_type` oder `Kontoart`

Fehlerhafte Zeilen werden protokolliert und brechen den Gesamtimport nicht ab.
