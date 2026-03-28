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
