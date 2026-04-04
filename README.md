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
