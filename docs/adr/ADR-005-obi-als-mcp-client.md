# ADR-005: Obi als MCP-Client für Systeme des Unternehmens

- **Status:** Angenommen
- **Datum:** 2026-10-31
- **Entscheider:** OpenBuchhaltung Kernteam
- **Umsetzung:** `app/services/mcp_client.py`, `mcp_oauth.py`, `mcp_connections.py`;
  `docs/umsetzungsplan.md`, Phase 8

## Kontext
Bisher spricht MCP nur in eine Richtung: Fremde Clients (ChatGPT, Claude, die iOS-App)
nutzen OpenBuchhaltung über den eigenen MCP-Server. Obi selbst kennt nur die eigenen
Tools. In Unternehmen liegen die Unterlagen, die Obi für die Buchhaltung braucht, aber in
anderen Systemen: Belege in der Ablage eines anderen Herstellers, Kunden und Projekte im
CRM, Richtlinien in einem Wiki. Viele dieser Systeme bieten inzwischen selbst einen
MCP-Server an.

Zwei Arten der Anmeldung kommen vor: Ein technischer Zugang für alle (der Administrator
richtet ihn einmal ein) oder der Zugang jedes Mitarbeiters, damit das fremde System
dessen Berechtigungen durchsetzt.

## Entscheidung

### 1. MCP-Client als Modul im Monolithen
- OpenBuchhaltung spricht fremde MCP-Server per **Streamable HTTP** (Protokoll 2025-06-18)
  an – wie der eigene MCP-Server nur mit der stdlib, synchron innerhalb des Chat-Zuges.
  Eine Sitzung je Server und Zug, `Mcp-Session-Id` und Protokollversion werden mitgeführt.
- Genutzt werden nur **Werkzeuge** (`tools/list`, `tools/call`). Nicht unterstützt: der
  alte HTTP+SSE-Transport, Ressourcen, Prompts und Anfragen des Servers an den Client
  (Sampling, Elicitation, Roots).
- Kein eigener Agenten-Dienst: Die Rechtebindung bleibt in-process (der Chat läuft mit
  dem Benutzerkontext), Konfiguration und Zugänge liegen in der App-Datenbank.

### 2. Server und Zugänge als Daten
- `mcp_server`: vom Administrator angelegt, instanzweit (nur globale Administratoren)
  oder je Mandant; Name, URL, Präfix, Hinweise für den Systemprompt, Anmeldeart (`none`,
  `token`, `oauth`), **Zugangsart** (`global` oder `user`), Freigabeliste der
  Werkzeuge, Bestätigungsregel, Zeitlimit, zwischengespeicherte Werkzeugliste.
- `mcp_credential`: zentraler Zugang (`user_id` leer, höchstens einer je Server, partieller
  Unique-Index) oder persönlicher Zugang je Benutzer. **Zentrale Zugänge verwaltet nur der
  Administrator, persönliche nur der Benutzer selbst.**
- Geheimnisse (Tokens, Client-Secrets, OAuth-Tokens, PKCE-Verifier) liegen
  Fernet-verschlüsselt mit einem aus `SECRET_KEY` abgeleiteten Schlüssel (eigener
  HKDF-Kontext) und werden nie ausgegeben – wie bei KI-Zugängen und Open Banking.

### 3. OAuth nach der MCP-Autorisierungsspezifikation
- Discovery über `WWW-Authenticate` bzw. Protected Resource Metadata (RFC 9728), dann
  RFC 8414/OIDC; ältere Server ohne Metadaten sind selbst Autorisierungsserver.
- Client: eingetragene Client-ID oder Dynamic Client Registration (RFC 7591) je
  Rücksprung-URL; PKCE S256, `resource` (RFC 8707), Refresh mit Rotation unter
  Zeilensperre.
- Der Rücksprung (`/obi-verbindungen/oauth/callback`) muss vom selben angemeldeten
  Benutzer kommen, der die Anmeldung begonnen hat – sonst könnte ein fremder Rücksprung
  einem Benutzer einen Zugang unterschieben. Endpunkte für Codes und Tokens nur über HTTPS
  (HTTP nur auf Loopback).
- Gegen untergeschobene Metadaten: Die Resource-Metadaten müssen zur URL des Servers
  gehören (RFC 9728, gleicher Ursprung, Verweis aus dem 401 nur vom selben Ursprung), die
  Metadaten des Autorisierungsservers dessen Aussteller nennen (RFC 8414), und `iss` im
  Rücksprung muss passen (RFC 9207) – alle Server teilen sich eine Rücksprung-URL.
- Verwirft der Autorisierungsserver eine selbst registrierte Client-ID
  (`invalid_client`), registriert die nächste Anmeldung neu.
- Die Weiterleitung zum Anbieter läuft über eine Zwischenseite mit Link, weil die CSP
  (`form-action 'self'`) Formular-Weiterleitungen auf fremde Hosts verbietet.

### 4. Sicherheitsmodell im Chat
- Werkzeuge heißen `<präfix>__<werkzeug>`; eigene Tools enthalten nie `__`.
- Nur Werkzeuge mit `readOnlyHint` laufen sofort, alle anderen über den bestehenden
  Bestätigungsablauf (Human-in-the-Loop). Der Administrator kann strenger („jeder Aufruf“)
  oder – nur für rein lesende Server – ohne Bestätigung arbeiten lassen und Werkzeuge per
  Freigabeliste begrenzen.
- Beschreibungen und Ergebnisse fremder Server sind nicht vertrauenswürdige Daten
  (gekennzeichnet wie Anhänge). Fehlertexte fremder Server gelangen nicht in den
  Systemprompt; Binärinhalte werden nur benannt.
- Die Verwaltungs-Tools der MCP-Server sind im Chat gesperrt: Obi erweitert seine
  Werkzeuge nicht selbst.
- Nicht lesende Aufrufe stehen im Audit-Log (Server, Werkzeug, maskierte Argumente),
  ebenso Anlegen, Ändern, Löschen und Zugänge.
- Die gemeinsame Werkzeugliste legen nur zentrale Zugänge oder der persönliche Zugang
  eines Administrators fest; die Anmeldung eines Benutzers prüft nur dessen Zugang. Sonst
  könnte ein Benutzer über sein Konto allen anderen Werkzeuge (samt `readOnlyHint`)
  unterschieben.
- Ein 403 (fehlende Rechte/Scope) lässt die Anmeldung bestehen; verworfen wird sie nur,
  wenn der Autorisierungsserver den Refresh endgültig ablehnt.

### 5. Verbindungen zu fremden Servern
- Keine Weiterleitungen (urllib schickte Anmelde-Header sonst an das neue Ziel mit),
  Link-Local-, Multicast- und reservierte Adressen gesperrt, Frist und Größengrenze je
  Aufruf für die ganze Antwort.
- Server mit Anmeldung nur über HTTPS (HTTP nur Loopback). Interne Adressen (privat,
  Loopback, CGNAT) darf nur ein globaler Administrator eintragen; Mandanten-Administratoren
  sollen darüber nicht das Netz der Instanz abtasten können. Restrisiko: ein Hostname, der
  nach dem Speichern auf eine interne Adresse umgestellt wird (DNS-Rebinding).
- Die URL sehen nur Administratoren, Logs und Audit nur ihren Ursprung: Gehostete Server
  tragen ihren Schlüssel oft im Pfad.

### 6. OpenBuchhaltung als MCP-Proxy (Nachtrag 2026-10-31)
- Je Server wählbar (`proxy_mode`: `off`, `read`, `all`), ob der eigene MCP-Server die
  Werkzeuge an seine Clients weiterreicht. Der MCP-Server bleibt ein Proxy auf die REST-API:
  `GET /mcp-proxy/tools` liefert die Werkzeuge im Format von `tools/list`,
  `POST /mcp-proxy/tools/call` führt sie mit dem Zugang des aufrufenden Benutzers aus. So
  gelten Rollen, Mandant und persönliche Zugänge wie überall; Geheimnisse verlassen die
  Instanz nicht, MCP-Clients sehen nur Werkzeuge und Ergebnisse.
- Keine Bestätigung durch Obi: Das übernimmt der MCP-Client. `read` reicht nur Werkzeuge mit
  `readOnlyHint` durch und ist die vorsichtige Wahl; Standard ist `off`.
- Der KI-Chat nutzt den Proxy nicht (er führt externe Werkzeuge selbst aus, mit
  Bestätigungspflicht); `MCPServer(proxy_external=False)`.
- Aufrufe über den MCP-Server tragen den Kanal `mcp` (Header bzw. Kontext der Bridge). Die
  REST-API kann damit Inhalte für MCP-Clients sperren, ohne Obi oder die Oberfläche zu
  treffen – zuerst das Firmenwissen je Gesellschaft (`knowledge_mcp_enabled`). Der Kanal
  kann nur einschränken, nie Rechte geben; den Schalter gibt es deshalb nicht als MCP-Tool.

## Konsequenzen
- Positiv: Obi erreicht Unterlagen und Funktionen anderer Systeme, ohne dass dafür
  Konnektoren in OpenBuchhaltung programmiert werden; Berechtigungen setzt bei
  persönlichen Zugängen das fremde System durch.
- Positiv: UI, REST-API und MCP-Server können die Anbindung verwalten (Parität); die
  iOS-App kann persönliche Anmeldungen über die API starten.
- Negativ: Jede Werkzeugdefinition kostet Kontext; viele Server oder Werkzeuge verdrängen
  Fachwissen (Begrenzung auf 50 Werkzeuge je Server, Freigabeliste).
- Negativ: Ein langsamer Server verlängert den Chat-Zug (Zeitlimit je Server, Abruf der
  Werkzeugliste höchstens stündlich).
- Negativ: Ein globaler Administrator kann interne Server eintragen; die Instanz erreicht
  damit auch das interne Netz. Einbinden nur vertrauenswürdiger Server.
- Negativ: Bei Servern mit Zugang je Benutzer braucht der Administrator einen eigenen
  Zugang, damit die Werkzeugliste abgerufen werden kann.
- Offene Punkte: Belege aus einem fremden System direkt übernehmen (Binärinhalte als
  Beleg statt nur als Hinweis), alter HTTP+SSE-Transport, Token-Widerruf beim Abmelden
  (RFC 7009), Werkzeuge auf Anforderung einblenden statt alle im Prompt.

## Alternativen
- **Eigener Obi-Dienst (Agentenschicht als separater Prozess):** entkoppelt, aber
  Rechtebindung, Konfiguration und Audit müssten über eine neue Schnittstelle laufen.
  Für die ersten Anbindungen zu schwer; bleibt möglich, wenn Hintergrundläufe dazukommen.
- **Fest programmierte Konnektoren je Hersteller:** pro System eigener Code und eigene
  Pflege; MCP macht das generisch.
- **Offizielles MCP-Python-SDK:** asynchron (anyio) und eine weitere Abhängigkeit; der
  benötigte Ausschnitt (JSON-RPC über HTTP, SSE lesen) ist klein und passt zum
  stdlib-Stil des eigenen MCP-Servers.
- **Nur zentrale Zugänge:** einfacher, aber das fremde System sähe jeden Zugriff als
  denselben technischen Benutzer – Berechtigungen und Nachvollziehbarkeit gingen
  verloren. Deshalb ist die Zugangsart je Server wählbar.
