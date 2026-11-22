# ADR-011: ELSTER-Übermittlung über ERiC

- **Status:** Angenommen
- **Datum:** 2026-11-22
- **Entscheider:** OpenBuchhaltung Kernteam

## Kontext
Bisher erzeugte OpenBuchhaltung für die Umsatzsteuer eine eigene XML-Nutzlast (`OpenBuchhaltungElster`) und gab
sie an einen externen Befehl (`ELSTER_ERIC_COMMAND`) weiter. Einen Befehl, der daraus eine amtliche Übermittlung
macht, gab es nicht.

Die Finanzverwaltung stellt für Steuersoftware den **ERiC** (ELSTER Rich Client) bereit: eine C-Bibliothek, die
Datensätze nach amtlichem Schema prüft (Schema- und Plausibilitätsprüfung), verschlüsselt, authentifiziert
überträgt und auf Wunsch ein PDF erzeugt. Erkenntnisse aus dem ELSTER-Entwicklerbereich (Stand November 2026):

- **Zugang:** Registrierung als Hersteller, danach Zugang zum Entwicklerbereich. Dort liegen ERiC, Dokumentation,
  Schemata, Testzertifikate und das Herstellerforum. Fragen zur Implementierung beantwortet ELSTER nur im Forum.
- **Releases:** Jährlich gibt es ein technisches Mai-Release und ein November-Release mit der
  Jahresfortschreibung (neue Anmeldezeiträume). Jeweils im April steigt die Mindestversion, ältere Versionen
  übermitteln dann nicht mehr. Seit dem 27.04.2026 ist das ERiC 43. Aktuell ist ERiC 44 (44.3.6.0); es bringt
  UStVA-Prüfmodule bis zum Jahr 2026 mit, 2027 kommt mit dem nächsten November-Release.
- **Plattformen:** ERiC gibt es unter anderem für Linux x86_64 (unser Produktionsbetrieb im Container) und macOS
  (lokale Entwicklung). Beigelegt sind Beispiele, auch eines in Python über `ctypes`.
- **Hersteller-ID:** Jeder Datensatz trägt eine fünfstellige Hersteller-ID, im TransferHeader und in Kennzahl 09.
  Sie gilt je Produkt und ist **auch für Testfälle Pflicht**; die frühere Beispiel-ID ist gesperrt. Für
  OpenBuchhaltung ist sie am 22.11.2026 beantragt, die Zuteilung steht aus.
- **Testbetrieb:** Testfälle tragen den Testmerker `700000004` und werden mit Testzertifikaten der ELSTER-Test-CA
  signiert; Fälle ohne Testmerker lehnt der Server dann ab. Für Tests gibt es Testfinanzämter.
- **Lizenz:** ERiC wird kostenlos überlassen und darf nur zusammen mit der eigenen Software als ein Produkt
  weitergegeben werden. Unterlizenzen sind nicht erlaubt, Testzertifikate dürfen nicht weitergegeben werden.
  Produkt- und Herstellername dürfen „ELSTER“ nicht enthalten. Der Hersteller muss die Nutzer vor der Nutzung auf
  den Datenschutz der Steuerverwaltung hinweisen (mit Bestätigung der Kenntnisnahme) und darauf, dass ERiC
  Protokolldateien schreibt – bei uns auf dem Server. Der Hersteller hält die Mindestversion ein und leistet den
  Support selbst.

## Entscheidung
1. **ERiC läuft in einem eigenen Prozess.** `app.services.eric` bindet die Bibliothek per `ctypes` an;
   geladen wird sie nur im Runner `python -m app.services.eric_runner`, den der ERiC-Transport für jede Prüfung
   und Übermittlung startet. ERiC hält globalen Zustand je Prozess; ein Absturz der C-Bibliothek trifft so nicht
   den Webprozess, und mehrere Gunicorn-Worker kommen sich nicht in die Quere. Der bisherige Vertrag über
   Umgebungsvariablen und Antwort-JSON bleibt; `ELSTER_ERIC_COMMAND` kann den integrierten Runner weiter ersetzen.
2. **ERiC und Testzertifikate liegen nicht im Repo und nicht im öffentlichen Image.** Der Betreiber lädt ERiC
   im Entwicklerbereich herunter und bindet das `lib`-Verzeichnis ein (`ELSTER_ERIC_LIBRARY_PATH`, im Container
   z. B. als Read-only-Volume). Das gilt auch für die ERiC-Dokumentation und die Schemata.
3. **Der ERiC-Transport übermittelt den amtlichen Datensatz.** `app.services.elster_xml` baut aus dem
   UStVA-Snapshot und den Firmenstammdaten den ElsterXML-Datensatz (Schnittstelle Version 11, Verfahren
   `ElsterAnmeldung`, Datenart `UStVA`, Vorgang `send-Auth`). Gespeichert und gehasht wird genau dieser Datensatz.
   Der Mock-Transport behält die interne Nutzlast.
   - Bemessungsgrundlagen in vollen Euro (Cent entfallen), Steuerbeträge auf Cent; Kennzahlen mit 0 entfallen,
     Kennzahl 83 steht immer. Die interne Zwischensumme „USt“ wird nicht übermittelt; die Steuer zu Kz 81/86/89/93
     rechnet ELSTER selbst.
   - Zeitraum: Monat `01`–`12`, Quartal `41`–`44`. Halbjahr und Jahr sind keine UStVA-Zeiträume.
   - ERiC verlangt Standard-Namensräume ohne Präfix; der Nutzdatenteil bringt seinen eigenen mit.
4. **Steuernummer im ELSTER-Format aus Bescheid-Steuernummer und Bundesfinanzamtsnummer.** Neues Feld
   `company.tax_office_number` (vierstellig, Migration 0068) in UI, REST und MCP. Die 13-stellige
   ELSTER-Steuernummer ist Bundesfinanzamtsnummer + `0` + die letzten acht Ziffern der Steuernummer laut
   Bescheid; das stimmt für alle Länderformate und deckt sich mit `EricMakeElsterStnr`. Eine schon 13-stellige
   Angabe wird übernommen. Die Bundesfinanzamtsnummer ist zugleich der Empfänger.
5. **Testmerker nach Umgebung.** `ELSTER_ENVIRONMENT=test` setzt den Testmerker, `production` nicht.
6. **Preflight prüft mit ERiC.** Ist ERiC eingebunden, prüft der Preflight den Datensatz lokal (ohne Senden) und
   meldet Fehler der Plausibilitätsprüfung feldgenau. Dafür reichen Hersteller-ID und Bibliothek; zum Senden
   braucht es zusätzlich Zertifikat und PIN (`ELSTER_CERTIFICATE_PATH`, `ELSTER_CERTIFICATE_PIN`).
7. **Erst die UStVA.** Die USt-Jahreserklärung (Verfahren `ElsterErklaerung`) wird über ERiC abgewiesen, bis ihr
   Datensatz gebaut ist.

## Konsequenzen
- Positiv: Der Datensatz ist gegen das echte ERiC geprüft – lokal mit der macOS-Bibliothek, im Preflight mit der
  eingebundenen. Fehler zeigt OpenBuchhaltung vor der Übermittlung feldgenau an. Ohne ERiC-Bibliothek laufen alle
  Tests weiter; der Integrationstest läuft nur mit `OBH_TEST_ERIC_LIBRARY`.
- Negativ: Jede Prüfung startet einen Prozess und initialisiert ERiC (etwa eine Sekunde). Zertifikat und PIN
  gelten je Instanz, nicht je Gesellschaft. Jedes November-Release verlangt neue Prüfmodule und eine Anpassung
  der unterstützten Jahre (`USTVA_MAX_YEAR`).
- Offene Punkte:
  - Hersteller-ID: Zuteilung abwarten, dann Testübermittlung mit Testzertifikat und Testmerker.
  - ERiC 45 (November-Release) für die UStVA 2027 einbinden, sobald freigegeben.
  - Hinweise an Nutzer aus dem Lizenzvertrag (Datenschutz mit Bestätigung, Protokolldateien) in UI, REST und MCP.
  - Zertifikat und PIN je Gesellschaft, verschlüsselt gespeichert, statt je Instanz.
  - Übermittlungsprotokoll als PDF (`ERIC_DRUCKE`) ablegen.
  - USt-Jahreserklärung, Zusammenfassende Meldung, Lohnsteuer-Anmeldung.
  - ERiC-Image für den Produktionsbetrieb (Linux x86_64) und Prüfung im Container.
  - Im Herstellerforum klären, wie sich eine Hersteller-ID mit einem Open-Source-Produkt verträgt, das andere
    selbst betreiben.

## Alternativen
- **ERiC im Webprozess laden:** weniger Aufwand je Aufruf, aber globaler ERiC-Zustand in jedem Gunicorn-Worker
  und ein Absturz der Bibliothek beendet den Worker. Verworfen.
- **Eigene Übermittlung ohne ERiC:** Die Annahmeserver erwarten den von ERiC verschlüsselten und signierten
  Datensatz; ERiC ist der vorgesehene Weg. Verworfen.
- **Fremder Übermittlungsdienst:** Steuerdaten liefen über einen Dritten, und die Abhängigkeit wäre dauerhaft.
  Verworfen.
- **Bundesland statt Bundesfinanzamtsnummer pflegen:** Die Steuernummernformate sind je Land verschieden und
  teils gleich lang; ohne Finanzamt fehlt zudem der Empfänger. Die Bundesfinanzamtsnummer liefert beides.
