# ADR-003: Native iOS-Begleit-App

- **Status:** Angenommen
- **Datum:** 2026-10-13
- **Entscheider:** OpenBuchhaltung Kernteam
- **Umsetzung:** `docs/umsetzungsplan.md`, Phase 6

## Kontext
OpenBuchhaltung ist eine Weboberfläche mit REST-API und MCP-Server. Sie läuft selbst
betrieben oder als gehostete Instanz. Mobile Arbeit in der Buchhaltung ist vor allem
Belegarbeit: Belege erfassen (Kassenbon, Bewirtung, PDF-Rechnung aus der Mail),
Bankumsätze zuordnen, TANs bestätigen, Zahlungen freigeben. Buchen mit mehreren Zeilen,
Abschluss, Lohn und Steuer bleiben Schreibtischarbeit.

Eine App im App Store macht OpenBuchhaltung außerdem für Menschen auffindbar, die nach
„Buchhaltung GmbH“, „Beleg scannen“ oder „UStVA“ suchen.

Vorhandene Grundlagen: Der OAuth-2.1-Server (PKCE S256, Dynamic Client Registration,
rotierende Refresh-Tokens) gibt Tokens aus, die für REST-API und MCP gleichermaßen
gelten. KI-Funktionen lösen ihren Endpoint je Benutzer über `resolve_llm_endpoint` auf.
Auch eigene OpenAI-`/responses`-kompatible Endpoints sind möglich, etwa ein lokaler
Modellserver.

## Entscheidung

### 1. Native Begleit-App statt Nachbau der Weboberfläche
- **SwiftUI-App für iPhone und iPad** in einem eigenen Repository
  (`openbuchhaltung-ios`). Bundle-ID und URI-Schema `de.openbuchhaltung.app`.
- Die App spricht ausschließlich die **REST-API** an (vierte Konsumentin neben UI, API
  und MCP; Paritätsregel). Fachlogik entsteht nie in der App.
- **Beliebige Instanzen:** Die App fragt nach der Server-URL und meldet sich per OAuth
  an: Discovery, Registrierung je Instanz, Login im System-Browser über
  `ASWebAuthenticationSession`, Redirect auf `de.openbuchhaltung.app:/oauth/callback`
  (RFC 8252). Tokens liegen in der Keychain. Mehrere Instanzen und Gesellschaften
  lassen sich nebeneinander nutzen (z. B. für Kanzleien).

### 2. Funktionsumfang in Stufen
| Stufe | Funktionen |
|---|---|
| **MVP** | Beleg-Scanner (VisionKit, Texterkennung auf dem Gerät, SHA-256 auf dem Gerät), Share Extension für PDFs aus Mail/Safari/Dateien, Inbox (unverbuchte Umsätze und Abgleich-Vorschläge per Wischgeste annehmen oder ablehnen), Übersicht über `GET /api/v1/dashboard`, Wechsel zwischen Instanzen und Gesellschaften |
| **1.x** | Push-Mitteilungen (neue Umsätze, überfällige OPOS, ELSTER-Status; später Fristen UStVA/ZM), TAN- und Open-Banking-Ablauf am Gerät, Freigaben mit Face ID (SEPA-Zahllauf, Lohnlauf, Festschreibung), Widgets (Bankstände, Liquidität, USt-Zahllast), Bewirtungs- und Reisebelege mit Pflichtangaben |
| **Später** | App Intents, Siri und Kurzbefehle („Kontostand Geschäftskonto?“), KI-Chat mit Bestätigung vor Aktionen, Erfassung ohne Netz mit Warteschlange |

**Bewusst nicht in der App:** Buchungserfassung mit mehreren Zeilen, Kontenrahmen,
Jahresabschluss, Lohn-Stammdaten, Steuererklärungen.

### 3. Server-Vorarbeit
- **Native Redirects:** Private-Use-URI-Schemata in Reverse-Domain-Form sind als
  Redirect zulässig. `OAUTH_ALLOWED_REDIRECT_HOSTS` beschränkt sie über ihren Namen
  (umgesetzt).
- **Dashboard-Endpunkt:** `GET /api/v1/dashboard` und MCP `get_dashboard` liefern
  Aufgaben, Bankstände und Kennzahlen in einem Aufruf (umgesetzt). Die Einträge der
  Inbox kommen aus den vorhandenen Listen-Endpunkten (`bank-transactions?status=open`,
  `receipt-match-suggestions`).
- **Push-Relay:** Selbst betriebene Instanzen können den APNs-Schlüssel nicht selbst
  halten. Ein zentraler Relay-Dienst (Vorbild Nextcloud) nimmt Mitteilungen von
  Instanzen entgegen, die sich mit einem Instanzschlüssel ausweisen. Er stellt nur
  einen neutralen Hinweistext zu, etwa „3 neue Bankumsätze“, ohne Beträge oder Namen.
  Die App holt die Details selbst. Instanzen melden Geräte über einen neuen Endpunkt
  `POST /api/v1/devices` an, mit Audit-Eintrag und Widerruf. Mitteilungen bleiben
  abschaltbar, die App funktioniert auch ohne sie. Instanzseitig umgesetzt:
  Ausgangswarteschlange, Dispatcher, Relay-Protokoll v1 (README, „Push-Mitteilungen“).
- **Mobile Belegerfassung und GoBD:** Erfassungszeitpunkt, Quelle und der auf dem Gerät
  berechnete Hash werden mit dem Beleg gespeichert. Der Server prüft den Hash beim
  Upload. Die Verfahrensdokumentation wird um „mobiles Erfassen/ersetzendes Scannen“
  ergänzt.

### 4. KI in drei Stufen, lokal zuerst
1. **Auf dem Gerät:** Texterkennung mit Vision und einfache strukturierte Extraktion
   (Betrag, Datum, Lieferant) mit Apples Foundation-Models-Framework. Ohne Netz, nichts
   verlässt das iPhone.
2. **Selbst betriebener Modellserver:** Ein OpenAI-`/responses`-kompatibler Server
   (z. B. Ollama, LM Studio oder vLLM; Kompatibilität vorab testen) auf eigener
   Hardware, angebunden als Instanz-Endpoint (`*_LLM_ENDPOINT_URL`) oder als Endpoint je
   Benutzer. Codeänderungen sind dafür nicht nötig. Belege werden asynchron mit
   Warteschlange verarbeitet, weil ein einzelnes Gerät nur wenige Anfragen parallel
   bearbeitet. Fällt der Server aus, warten die Belege. Auf einen Cloud-Anbieter wird
   nur ausgewichen, wenn das ausdrücklich konfiguriert ist.
3. **Cloud-Anbieter:** nur mit eigenem Schlüssel und ausdrücklicher Konfiguration
   (heute schon möglich).

### 5. App-Store-Präsenz
- **Demo-Modus ohne Konto:** Die App verbindet sich mit einer Demo-Instanz, deren Daten
  nachts zurückgesetzt werden (Seed wie `seed-demo`). Dieselbe Instanz dient als
  Testzugang für die App-Review.
- **Onboarding für Selbstbetreiber:** Hinweis auf die Open-Source-Installation.
- Falls Konten direkt in der App angelegt werden können, muss sich das Konto nach
  Apple-Richtlinie 5.1.1(v) in der App auch löschen lassen (mit Export der
  Buchhaltungsdaten und Hinweis auf die Aufbewahrungspflichten).
- **Store-Präsenz:** Keywords (Buchhaltung, GmbH, UG, Beleg scannen, UStVA, ELSTER,
  DATEV, Open Source), Screenshots von Scanner und Inbox, Smart App Banner auf
  openbuchhaltung.de.

## Konsequenzen
- **Positiv:**
  - Die Belegerfassung, der häufigste Engpass, wird deutlich einfacher.
  - OpenBuchhaltung wird im App Store auffindbar.
  - KI-Verarbeitung ist ohne Cloud möglich.
  - Die Server-Vorarbeit (Dashboard, native OAuth) nutzt auch anderen Clients.
- **Negativ:**
  - Zweite Codebasis (Swift) mit eigenem Release-Zyklus und App-Review.
  - Dauerhafter Betrieb von Push-Relay und Demo-Instanz.
  - Die API wird zur öffentlichen Vertragsfläche. Breaking Changes brauchen eine
    Versionierung (`/api/v1` bleibt stabil, Neues kommt hinzu).
- **Offene Punkte:**
  - Modellauswahl und Benchmark für die Belegextraktion auf lokaler Hardware.
  - Selbstregistrierung und Mandantenanlage ohne Admin (heute legt ein Admin Mandanten an).
  - Android erst nach der iOS-Validierung.

## Alternativen
- **Nur PWA:** Das wäre günstiger, aber ohne Dokumentenscanner auf Systemniveau, Share
  Extension, verlässliche Push-Mitteilungen, Widgets und App Intents, und nicht im App
  Store auffindbar. Verworfen.
- **Plattformübergreifend (Flutter/React Native):** Android käme gleich mit. VisionKit,
  Foundation Models, Widgets und App Intents bräuchten aber ohnehin nativen Code.
  Verworfen für die erste Version, neu bewerten, sobald Android nachgefragt wird.
