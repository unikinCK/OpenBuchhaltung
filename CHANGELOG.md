# Changelog

Alle nennenswerten Änderungen an OpenBuchhaltung werden hier festgehalten.
Das Format folgt [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), die
Versionierung [SemVer](https://semver.org/lang/de/). Releases tragen den Git-Tag
`v<Version>`; die Version steht in `pyproject.toml` (`[project] version`) und wird
über `GET /api/v1/health` sowie im Prüferexport-Manifest ausgegeben.

## [Unreleased]

### Projekt-Webseite: App für iPhone und iPad
- Smart App Banner: Startseite, Impressum und Datenschutzerklärung tragen
  `<meta name="apple-itunes-app" content="app-id=6826612646">`, damit Safari auf iPhone und
  iPad auf die App im App Store hinweist – ohne Skript, passend zur strikten CSP.
- Neuer Abschnitt „App für iPhone und iPad“ (`#app`, auch in der Navigation) auf der
  Startseite: Belege scannen, Inbox, KI-Chat, Übersicht und Spalten auf dem iPad, dazu
  Anmeldung und Datenschutz mit Link auf `datenschutz.html#app` und auf die App im App Store.
- `tests/test_website.py` prüft Banner und App-Store-Link. Ausgeliefert wird die Webseite
  erst, wenn Apple die App freigegeben hat.

### Datenschutzerklärung für die iOS-App
- `website/datenschutz.html` hat einen neuen Abschnitt „App für iPhone und iPad“ (Anker
  `#app`, verlinkt aus der App und als Datenschutz-URL im App Store): Die App spricht nur
  mit der gewählten Instanz, ohne Tracking und Drittanbieter-Bausteine; was sie überträgt
  (Anmeldung per OAuth, Belege mit Erfassungsangaben und Prüfsumme), was auf dem Gerät
  bleibt, KI-Funktionen erst nach Zustimmung, Demo-Instanz und App Store. Rechte und
  Änderungen rücken auf 6 und 7.

### Übersicht eine Ebene tiefer (iOS-App) und alle Bankkonten im Dashboard
- **Bankstände aller Geldkonten:** `GET /api/v1/dashboard` (und MCP `get_dashboard`)
  listete nur Konten mit importierten Umsätzen; Tages-, Festgeld- oder Girokonten, die
  ohne Kontoauszugsimport gebucht werden, fehlten. Jetzt zählen auch Konten mit
  Bankverbindung (FinTS, Open Banking) und – bei erkennbarem Kontenrahmen – bebuchte
  Kassen- und Bankkonten (SKR03 1000–1299, SKR04 1600–1899; Geldtransit nicht). Neu je
  Konto: `transaction_count` (importierte Umsätze) und `latest_entry_date` (letzte
  Buchung).
- **Behoben:** Der Buchsaldo im Saldenabgleich je Bankkonto (Bankseite,
  `GET /api/v1/bank-reconciliation`, Dashboard) zählte nach einem Jahresabschluss die
  Saldovorträge doppelt; sie wiederholen nur die Vorjahre und bleiben jetzt außen vor
  (wie in Kontoblatt und Bilanz).
- **Filter für den Drilldown:** `GET /api/v1/bank-transactions?bank_account_id=…`
  (Umsätze eines Kontos; UI: Bankseite mit Absprung aus dem Saldenabgleich),
  `GET /api/v1/documents?unlinked=1` mit `limit`/`offset` und `total` (aktuelle Belege
  ohne Buchung), `GET /api/v1/open-items?overdue=1` (UI: Schalter „Überfällige“ auf der
  OPOS-Seite); alle drei auch in den MCP-Tools. Die Dashboard-Kacheln der
  Weboberfläche springen direkt in die gefilterten Listen.
- **Belege ohne Buchung** zählen nur noch aktuelle Fassungen: Eine ersetzte Version
  blieb sonst dauerhaft „ohne Buchung“, auch wenn ihre Nachfolgerin verknüpft war
  (Dashboard-Kachel, Beleg-Seite, API).
- **Belegabgleich-Vorschläge** liefern zusätzlich den Beleg (`document`), den
  Bruttobetrag der vorgeschlagenen Buchung (`journal_entry.amount`) und im
  `booking_proposal` Kontonummer/-bezeichnung, Steuercode, Partnername und das
  vorgeschlagene Gegenkonto (`creditor_account_id`, Verbindlichkeiten aLuL wie in der
  Freigabemaske) – Grundlage für Prüfen und Freigeben in der iOS-App.
- **Demo-Beleg:** Die Musterrechnung der Demo-Daten ist jetzt ein vollständiges PDF mit
  Querverweistabelle; PDF-Betrachter wie die Belegvorschau der App zeigten bisher eine leere
  Seite.

### MCP: Zahlen in Textfeldern werden als Text weitergegeben
- MCP-Clients, die Typ-Unions wie `["string", "null"]` nicht auswerten, schickten
  ziffernartige Texte als JSON-Zahl; die API lehnte etwa eine PLZ beim Anlegen eines
  Partners mit „postal_code muss ein Text sein.“ ab. Der MCP-Server wandelt Zahlen in
  Feldern, die laut Tool-Schema Text sind, jetzt vor dem API-Aufruf in Text um (auch in
  verschachtelten Objekten und Listen, z. B. Buchungszeilen). Die REST-API bleibt streng.

### KI-Chat: Reasoning-Schranke statt Gesamt-Zeitlimit
- Ein Modell, dessen Denkprozess in eine Endlosschleife geriet, blockierte den KI-Server
  bis zum Abbrechen in der App oder bis zum Zeitlimit von 20 Minuten – bei lokalen
  Servern, die nur eine Anfrage gleichzeitig bearbeiten, für alle Benutzer.
- Neu: `CHAT_LLM_MAX_REASONING_TOKENS` (Standard `16000`, `0` = aus) begrenzt den
  Denkprozess je Modellaufruf. Gezählt wird im Stream, unabhängig davon, wie das Modell
  den Denkprozess liefert: getypte Reasoning-Ereignisse (`response.reasoning_text.delta`,
  `response.reasoning_summary_text.delta`) und `<think>`-Text in der Antwort. Die
  gesamte Ausgabe je Aufruf ist zusätzlich auf das Budget + 8000 Token begrenzt; das
  fängt Modelle, deren `<think>` schon im Prompt-Template steht und deren Denkprozess
  deshalb erst am `</think>` erkennbar ist.
- Dieselbe Obergrenze geht als `max_output_tokens` an den Endpoint (Anbieter mit
  verdecktem Reasoning wie OpenAI o-/gpt-5-Modelle; LM Studio zählt den Denkprozess mit).
  Ein am Limit abgeschnittener Aufruf (`status: incomplete` mit Grund
  `max_output_tokens`, bei LM Studio `usage.output_tokens` am Limit) gilt nicht mehr als
  leere Antwort.
- Bei Überschreitung schließt der Chat die Verbindung (der Server hört auf zu
  generieren) und meldet „Obi hat zu lange nachgedacht und wurde abgebrochen – bitte die
  Frage neu oder genauer stellen.“ – in der Weboberfläche, im NDJSON-Stream der REST-API
  (iOS-App) und über MCP (`send_chat_message`). Der Hinweis wird wie ein Abbruch als
  Antwort im Verlauf gespeichert, damit die abgebrochene Frage beim nächsten Zug nicht
  unbeantwortet im Verlauf steht.
- **Geändert:** `CHAT_LLM_TIMEOUT_SECONDS` entfällt (wird ignoriert, Warnung beim
  Start). Gegen tote Verbindungen gilt nur noch ein Leerlauf-Timeout,
  `CHAT_LLM_IDLE_TIMEOUT_SECONDS` (Standard `600`): Abbruch, wenn das Modell so lange
  keine Daten liefert. `LLM_TIMEOUT_SECONDS` (Beleg-OCR usw.) bleibt unverändert.

### KI-Chat: Obi, der KI-Buchhalter
- Der Chat-Assistent heißt jetzt Obi und hat ein eigenes Logo: Nickelbrille,
  Scheitel, Pullunder mit Rautenmuster und Bleistift hinterm Ohr
  (`app/static/obi-icon.svg`, mit Wortmarke `app/static/obi-logo.svg`).
- Assistenten-Nachrichten zeigen Obi statt „KI“ als Avatar; die Begrüßung im
  leeren Chat stellt Obi mit Logo vor.
- Der System-Prompt nennt den Namen, damit sich der Assistent auf Nachfrage als
  Obi vorstellt.

### KI-Chat: Konten gezielt abfragen, gekürzte Ergebnisse nicht mehr als „fehlt“ werten
- „Rufe die Buchungen auf Konto 4985 ab“ endete mit „Konto 4985 gibt es nicht“: Der Chat
  rief den ganzen Kontenplan ab, das Ergebnis wurde für das Modell nach 20.000 Zeichen
  abgeschnitten (bei gut 70 Konten etwa ab Kontenklasse 3), und das Modell schloss aus
  dem Fehlen, dass das Konto nicht existiert.
- `GET /api/v1/accounts` bzw. das MCP-Tool `list_accounts` filtern jetzt nach `code`
  (exakte Kontonummer), `q` (Teiltreffer in Nummer/Bezeichnung) und `account_type` –
  wie die Suche auf der Kontenseite.
- Neuer Endpoint `GET /api/v1/accounts/ledger` mit `account_id` oder
  `company_id` + `account_code`; `get_account_ledger` nimmt damit auch die Kontonummer
  direkt. `/api/v1/accounts/<id>/ledger` bleibt unverändert.
- Der Chat gibt Tool-Ergebnisse als kompaktes JSON ans Modell (gut ein Drittel weniger
  Zeichen); die Anzeige rückt weiterhin ein, auch bei abgeschnittenen Ergebnissen.
- Gekürzte Ergebnisse tragen einen deutlichen Hinweis („nur die ersten … von … Zeichen,
  UNVOLLSTÄNDIG – was fehlt, kann trotzdem existieren, mit Filtern eingrenzen“); der
  System-Prompt weist zusätzlich auf gezielte Abfragen hin.

### E-Rechnungen mit mehreren Steuersätzen
- Der E-Rechnungs-Import (XRechnung/ZUGFeRD) bucht Rechnungen mit mehreren
  Steuersätzen, z. B. 7 % und 19 %, jetzt je Satz: eine Aufwands- und eine
  Vorsteuerzeile pro Satz aus der Steueraufschlüsselung des XML. Bisher wurde die
  ganze Steuer mit einem Steuercode gebucht.
- Steuercode je Satz: Der gewählte Steuercode gilt für den Satz, zu dem er passt.
  Für die übrigen Sätze wird der Vorsteuer-Code mit diesem Satz ermittelt.
  `POST /api/v1/einvoices/import` und das MCP-Tool `import_einvoice` nehmen
  `tax_lines` an (`rate` mit `tax_code_id` und/oder `expense_account_id`). Die
  Weboberfläche hat dafür eine Auswahl je Steuersatz. Rechnungen mit einem Satz
  werden wie bisher gebucht.
- Passt die Summe der Steueraufschlüsselung nicht zum Rechnungsbetrag, wird die
  Rechnung nicht gebucht (HTTP 422).
- ZUGFeRD 1 (`CrossIndustryDocument`): Der Steuersatz aus `ApplicablePercent` wird
  jetzt gelesen.

### Projekt-Webseite ohne GitHub-Links
- Das Repository ist nicht mehr öffentlich. Die Webseite verlinkt deshalb nicht mehr auf
  GitHub (Kopfzeile, Hero, Fußzeile, README-Hinweis, Abschnitt „Mitmachen“ und die
  zugehörige FAQ). Die Installationsbefehle nennen `<Repository-URL>` statt der
  GitHub-Adresse; Impressum und Datenschutzerklärung erwähnen GitHub nicht mehr.

### Belegbuchung mit mehreren Steuersätzen und Trinkgeld
- Beleg-OCR und Belegabgleich buchen Belege mit mehreren Steuersätzen jetzt in einem
  Schritt, z. B. Restaurant 100,00 € netto zu 7 % plus 10,00 € netto zu 19 % oder
  Hotel mit Übernachtung und Frühstück. Je Satz entstehen eine Aufwands- und eine
  Vorsteuerzeile. Bisher gab es dafür nur den Hinweis „Bitte je Steuersatz buchen“.
- Der Kontierungsvorschlag (`booking_proposal.tax_lines`) enthält die Aufteilung samt
  Vorsteuer-Code je Satz. `POST /api/v1/receipt-ocr/book` und
  `POST /api/v1/receipt-matching/suggestions/<id>/approve` nehmen sie als `tax_lines`
  an, ebenso die MCP-Tools `book_receipt_ocr_suggestion` und
  `approve_receipt_match_suggestion`. Fehlt der Steuercode einer Zeile, gilt der
  Vorsteuer-Code zum angegebenen `rate`. Ein abweichendes Aufwandskonto je Satz ist
  möglich.
- Trinkgeld (`tip_amount`) wird als eigene Aufwandszeile ohne Umsatzsteuer gebucht
  und erhöht den Betrag an den Kreditor.
- Die Buchungsmasken zeigen bei mehreren Sätzen eine Tabelle mit je einer Zeile pro
  Satz plus einer Leerzeile für einen weiteren Satz. Das Trinkgeld ist vorbelegt.
- Beleg-OCR und Belegabgleich nutzen dafür dieselbe Buchungsfunktion
  (`book_incoming_invoice`).
- Migration `20261018_0051` speichert die Aufteilung am Belegabgleich-Vorschlag.

### Beleg-OCR und Belegabgleich: bessere Vorschläge, Kontierungsvorschlag
- Die KI-Extraktion hatte das Ergebnis des Modells nur in leere Felder übernommen.
  Falsche Treffer der Schlüsselwortsuche blieben deshalb stehen (USt-IdNr.
  `DE123456789` als Steuerbetrag, „Page 1 of 1“ als Lieferant). Jetzt werden beide
  Ergebnisse auf Plausibilität geprüft: Stimmt Netto + Steuer = Brutto, liegt die
  Steuerquote unter 28 % und steht der Bruttobetrag im Text, gelten die KI-Beträge
  (Status „korrigiert“). Lieferant, USt-IdNr. und Beschreibung kommen bevorzugt von
  der KI.
- Neuer Prompt mit klaren Regeln: Rechnungssteller statt Empfänger, Rechnungs- statt
  Fälligkeitsdatum, Anzahlungen mindern den Rechnungsbetrag nicht. Neue Felder:
  Aufteilung je Steuersatz (`tax_lines`, z. B. 7 % Speisen / 19 % Getränke),
  Trinkgeld, Reverse Charge (§ 13b) und eine kurze Leistungsbeschreibung.
  Temperatur 0 und JSON-Schema (`text.format`). Lehnt der Server das ab (HTTP 400),
  wird ohne beides wiederholt.
- **Kontierungsvorschlag:** Konto, Steuercode und Geschäftspartner werden
  vorbelegt. Grundlage ist die letzte Buchung desselben Lieferanten (Partner per
  USt-IdNr. oder Name, sonst Buchungstext). Gibt es keine, wählt die KI ein Konto
  aus dem Kontenplan (Aufwandskonten plus Anlagekonten, keine AfA-Konten). Das
  liefern `POST /api/v1/receipt-ocr/suggestions` und der Belegabgleich als
  `booking_proposal`, mit Begründung. In der Weboberfläche ist der Vorschlag
  vorausgewählt. Die Freigabe einer neuen Buchung übernimmt ihn, wenn Konto oder
  Steuercode fehlen. Der Partner steht auf den Buchungszeilen (`partner_id` auch
  bei `book_receipt_ocr_suggestion`).
- Regelbasiert: Ziffern aus Kennungen (USt-IdNr., Rechnungsnummern) zählen nicht
  mehr als Betrag. „inkl. MwSt.“ verweist nicht mehr auf den Steuerbetrag. Daten
  vor 2000 oder mehr als ein Jahr in der Zukunft werden ignoriert. Adress- und
  „Feld: Wert“-Zeilen gelten nicht als Lieferant.
- Belegabgleich: Kandidaten passen auch auf Brutto + Trinkgeld (Kartenzahlung im
  Restaurant).
- Migration `20261018_0050` ergänzt die Vorschlagsfelder am Belegabgleich-Vorschlag.

### KI-Chat: 20 Minuten Zeit und Live-Fortschritt aus dem Modell
- Lange Antworten lokaler Modelle brachen mit „Chat-LLM-Endpoint antwortete nicht
  rechtzeitig.“ ab (120 s je Modellaufruf, ohne Rückmeldung bis zur fertigen Antwort).
- Der Chat holt die Modellantwort jetzt gestreamt (`stream: true` am
  `/responses`-Endpoint; Endpoints ohne Stream antworten weiter mit JSON). Der
  Fortschritts-Stream meldet zusätzlich `generating` (Phase Denken/Antworten,
  Token-Zähler, Ausschnitt des Denkprozesses) und `preparing_tool`; Abbrechen stoppt
  den Modellaufruf jetzt sofort statt erst nach dessen Ende.
- Die Weboberfläche zeigt „Denkt nach … · 528 Tokens“ mit dem aktuellen Gedanken,
  „Schreibt die Antwort …“, „Bereitet „…“ vor …“ und nach 30 s ohne neue Daten
  „letzte Rückmeldung vor …“.
- `CHAT_LLM_TIMEOUT_SECONDS` (neu Standard `1200`) begrenzt jetzt die Gesamtdauer einer
  Chat-Nachricht samt aller Modell- und Tool-Aufrufe statt jedes einzelnen Aufrufs.

### KI-Chat: Fortschritt auch über die REST-API
- `POST /api/v1/chat/messages` und `/api/v1/chat/actions/<id>/confirm|reject` liefern mit
  `Accept: application/x-ndjson` denselben Fortschritts-Stream wie die Weboberfläche
  (Keep-alive alle 5 s, Abbrechen durch Schließen der Verbindung). Damit zeigt die
  iOS-App den Chat-Fortschritt live und läuft bei langen Antworten nicht in Timeouts.
  Ohne den Header bleibt die JSON-Antwort unverändert.

### Betrieb: gunicorn mit gthread-Workern
- Ein KI-Chat, der länger als 180 s dauerte (lokale Modelle, mehrere Tool-Runden), brach
  mitten in der Antwort ab: Der sync-Worker wurde nach `--timeout` hart beendet, der
  Browser zeigte „Load failed“, nginx meldete „upstream sent invalid chunked response“.
- Der Produktions-Stack (`docker-compose.production.yml`, `Dockerfile`) läuft jetzt mit
  `--worker-class gthread --threads ${GUNICORN_THREADS:-4}`. `--timeout` gilt damit nur
  noch für hängende Worker-Prozesse, nicht für einzelne lange Requests; lange Chats
  blockieren außerdem keinen ganzen Worker mehr.

### KI-Chat: Denkprozess nicht mehr in der Antwort
- Bei LM Studio (Nemotron) stand vereinzelt der Denkprozess des Modells („Der Benutzer
  fragt nach …“) als eigener Textteil vor der eigentlichen Antwort. Der Chat übernimmt
  jetzt nur noch die letzte Antwort-Nachricht, verwirft Zwischen-Nachrichten mit
  `phase="commentary"` und entfernt `<think>…</think>`-Text bzw. alles vor einem
  einzelnen `</think>`.
- Antworten mit mehreren Textteilen werden mit ihrer Struktur (Typen, Längen, kein
  Inhalt) ins Log geschrieben, um weitere Formate zu erkennen.

### KI-Chat: Fortschritt und Abbrechen
- Während der Antwort zeigt der Chat live, was passiert („Denkt nach …“, „Bankumsätze
  durchsuchen …“, „Wertet die Ergebnisse aus …“), die erledigten Schritte und die
  verstrichene Zeit; die eigene Nachricht erscheint sofort.
- „Abbrechen“ beendet die Antwort; im Verlauf steht danach „Antwort abgebrochen.“. Bei
  bestätigten Aktionen ist Abbrechen erst möglich, wenn die Aktion ausgeführt ist.
- Technisch: `/chat/send` und `/chat/actions/<id>/confirm|reject` streamen mit
  `Accept: application/x-ndjson` (Zug im Hintergrund-Thread, Keep-alive alle 5 s,
  `X-Accel-Buffering: no`); ohne den Header bleibt es bei JSON.

### KI-Chat: Layout, Umbenennen, Handy
- Helle Seitenleiste, Unterhaltungen nach Heute/Gestern/Letzte 7 bzw. 30 Tage/Älter
  gruppiert, Titel zweizeilig statt abgeschnitten. Umbenennen und Löschen direkt am
  Eintrag, auf Touch-Geräten immer sichtbar.
- Neu: Unterhaltung umbenennen in UI, REST (`POST /api/v1/chat/conversations/<id>/rename`)
  und MCP (`rename_chat_conversation`); die Reihenfolge der Liste bleibt dabei erhalten.
- Kopfzeile mit dem Titel der Unterhaltung; auf schmalen Bildschirmen klappt die
  Unterhaltungsliste über „☰ Unterhaltungen“ auf, der Verlauf nutzt die volle Breite.
- Startseite mit anklickbaren Beispielfragen und „Beleg hochladen“; kürzerer Platzhalter,
  `aria-live` für neue Nachrichten.

### KI-Chat: verständliche Tool-Aufrufe und Bestätigungen
- Jedes Tool hat einen deutschen Anzeigenamen (`app/services/tool_titles.py`); der
  MCP-Server liefert ihn als `title` aus.
- Die Schritte einer Antwort stehen zugeklappt in einer Zeile („2 Schritte · Konten
  auflisten · Buchung erfassen“); Ergebnisse ohne interne Fremddaten-Markierung, JSON
  eingerückt.
- Die Bestätigungsbox zeigt die Aktion als Titel, die Argumente als beschriftete Felder
  und Buchungszeilen als Tabelle (Beträge, Datum im deutschen Format); das rohe JSON
  steht unter „Technische Details“.

### KI-Chat: formatierte Antworten
- Antworten des Assistenten werden als Markdown dargestellt: Tabellen (Beträge
  rechtsbündig, waagerecht scrollbar), Listen, Überschriften, fett/kursiv, Code und
  Zitate. Der Renderer (`app/static/chat_markdown.js`) baut DOM-Knoten direkt und
  reicht nie HTML durch; anklickbar sind nur Links auf Pfade der App.
- „Tabelle kopieren“ (tabulatorgetrennt, z. B. für Excel) und „Antwort kopieren“.
- Der System-Prompt weist das Modell auf die Markdown-Darstellung hin.

### Online-Demo auf der Projekt-Webseite
- Neuer Abschnitt „Online-Demo“ mit Zugang, Demo-Daten, Sperren und Anleitung zum
  Verbinden des Demo-MCP (Claude, ChatGPT, Claude Code); Hero-Button und FAQ verweisen
  darauf.

### Demo-Instanz hinter vorhandenem nginx
- Neues Overlay `docker-compose.demo-proxy.yml`: Der nginx einer öffentlichen Instanz
  liefert zusätzlich die Demo-Instanz (eigenes Compose-Projekt) unter `DEMO_DOMAIN` mit
  eigenem Let's-Encrypt-Zertifikat aus. nginx tritt dem Netz des Demo-Projekts bei
  (`DEMO_NETWORK`) und löst `DEMO_UPSTREAM` zur Laufzeit auf; eine gestoppte Demo legt
  die übrigen Domains nicht lahm.
- certbot und der Reload-Hook von nginx berücksichtigen `DEMO_DOMAIN`.
- Demo-MCP: `COMPOSE_PROFILES=mcp` startet im Demo-Stack den MCP-Server, nginx leitet
  `https://<DEMO_DOMAIN>/mcp` an `DEMO_MCP_UPSTREAM` weiter. Ohne Server-Token nimmt er
  nur Benutzer- bzw. OAuth-Tokens an (Connector-Login `demo`/`demo1234`); die
  Demo-Sperren gelten auch für MCP-Tools (Test).
- Behoben: Mit dem Demo-Proxy landete die eigene Domain der Instanz auf der Demo, weil
  auch deren App im mitgenutzten Netz `app` heißt. nginx erreicht App und MCP der
  eigenen Instanz jetzt über eindeutige Aliase (`obh-instance-app`, `obh-instance-mcp`).

### Lokale KI-Server (LM Studio, Ollama)
- Neu: Modellname `loaded` (Instanz-Umgebung und KI-Zugang je Benutzer) verwendet das
  Modell, das der Server beim Aufruf gerade geladen hat (LM Studio `/api/v1/models` bzw.
  `/api/v0/models`, Ollama `/api/ps`). OpenBuchhaltung lädt oder wechselt dadurch nie
  selbst ein Modell; ist keines geladen, schlägt der Aufruf fehl.
- Beleg-OCR: Lehnt der Endpoint PDFs als Datei ab (HTTP 400, z. B. LM Studio), werden die
  Seitenbilder des Scans (JPEG direkt, sonst verlustfrei als PNG) als Bilder geschickt.
- Neu: `LLM_TIMEOUT_SECONDS` für Beleg-OCR, Feldextraktion, Belegabgleich und
  Dokument-Update (bisher fest 30 bzw. 20 s); Lese-Timeouts der LLM-Aufrufe enden jetzt
  mit einer verständlichen Meldung statt einem Serverfehler.
- KI-Chat: Reasoning-Elemente der Antwort (LM Studio liefert sie mit `reasoning_text`)
  erscheinen nicht mehr als Teil der Chat-Antwort.
- MCP: `send_chat_message`, `confirm_chat_action` und `create_receipt_match_suggestion`
  warten bis zu 175 s auf die App statt 15 s (mehrere LLM-Runden mit lokalen Modellen).

### Belege per Link (`source_url`)
- Neu: `upload_document`, `replace_document` und `create_receipt_ocr_suggestion` (MCP und
  REST, JSON-Body) nehmen statt `content_base64` auch `source_url` an; der Server lädt die
  Datei dann selbst, statt sie als Base64 durch das Sprachmodell zu schleusen (z. B.
  `downloadUrl` eines Mail-Anhangs aus mcp-imap).
- Abgeschaltet, solange `DOCUMENT_FETCH_ALLOWED_HOSTS` leer ist. Nur http(s), Host:Port
  auf der Allowlist, Weiterleitungen nur innerhalb des Hosts, Zeitlimit
  (`DOCUMENT_FETCH_TIMEOUT_SECONDS`, Standard 30 s), gestreamtes Größenlimit, interne
  Adressen nur für exakt eingetragene Hosts.
- `file_name`/`mime_type` werden bei `source_url` aus `Content-Disposition` und
  `Content-Type` abgeleitet; `client_sha256` wird gegen die geladene Datei geprüft, jetzt
  auch bei `replace_document`. Fehler nennen den Grund (`source_url_error`) und den
  Rückfall `content_base64`. Das Audit-Log vermerkt `source_host`.

### Einheitliches Logo
- Neues Logo für Web-App, Webseite und iOS-App: das T-Konto mit „O“ und „B“ unter den
  Querstrichen (`app/static/favicon.svg`, `website/assets/favicon.svg`).
- Die Web-App zeigt das Logo in der Kopfzeile und liefert Favicon und Apple-Touch-Icon aus;
  die Webseite hat neue Favicons und ein aktualisiertes Vorschaubild (`og-image.png`).

### Öffentliche Demo-Instanz
- Neu: `DEMO_MODE` für eine öffentliche Demo (z. B. demo.openbuchhaltung.de):
  - Banner „Demo-Instanz“ und vorbelegter Login `demo`/`demo1234`
  - gesperrt (UI und API, Antwort 403): Benutzer- und Mandantenverwaltung,
    Passwortänderung, KI-Zugang je Benutzer, FinTS/Open Banking, ELSTER-Übermittlung
- Neu: `flask demo-reset` leert Datenbank und Uploads und spielt die Demo-Daten neu ein;
  `--loop` wiederholt das täglich zu `DEMO_RESET_TIME`. PostgreSQL wird per `TRUNCATE`
  geleert (die GoBD-Trigger sperren einzelne Löschungen; das Schema bleibt erhalten),
  SQLite neu migriert. Compose-Overlay `docker-compose.demo.yml` mit Dienst `demo-reset`.
- `seed-demo` legt zusätzlich Alltag an: gebuchte und offene Bankumsätze mit
  Buchungsvorschlägen („wie zuletzt“, Regel „Google Ads“), einen überfälligen offenen
  Posten und einen Beleg ohne Buchung. Die Demo-Logik liegt jetzt in
  `app/services/demo.py`.

### Buchungsvorschlag je Bankumsatz
- Neu: Offene Bankumsätze bekommen einen Buchungsvorschlag aus passender Regel, aus
  einem Eigenübertrag (Geldtransit) oder „wie zuletzt“ aus der letzten Bankbuchung
  derselben Gegenseite mit gleichem Vorzeichen (Gegenkonto, Steuercode, Kontierung,
  Partner).
- Die Bank-Seite belegt den Buchungsdialog damit vor und zeigt den Hinweis
  „Wie zuletzt“.
- `GET /api/v1/bank-transactions?include_suggestions=1` und MCP
  `list_bank_transactions` liefern je offenem Umsatz `booking_proposal`. Regel-Treffer
  waren bisher nur in der Weboberfläche sichtbar.

### Zeitangaben in deutscher Ortszeit
- Behoben: Die Oberfläche zeigte gespeicherte Zeitpunkte (UTC) unverändert an, ein um
  17:42 MESZ aufgenommener Beleg erschien als 15:42. Die Filter `datum_zeit` und
  `datum` rechnen Zeitpunkte jetzt nach Europe/Berlin um (Sommer- und Winterzeit);
  naive Werte aus SQLite gelten als UTC, reine Datumswerte bleiben unverändert.
- Einheitlich über die Filter statt eigener `strftime`-/ISO-Ausschnitte: Belegabgleich,
  Audit-Log, Controlling-Historie, UStVA-/Ertragsteuer-Übermittlungen, OAuth-Zugriffe
  und Push-Geräte, FinTS-Bankenliste, Open-Banking-Zustimmung. Audit-Log und
  Controlling zeigen dabei das deutsche Format mit Sekunden.
- Gespeicherte Werte, API-Ausgaben (ISO mit UTC) und Exporte bleiben unverändert.

### Mobile Belegerfassung (Server-Vorarbeit III)
- Neu: Belege speichern ihren Erfassungsweg (`capture_source`: web, api, einvoice,
  mobile_scan, mobile_share), bei mobiler Erfassung zusätzlich Aufnahmezeitpunkt und
  Gerät (`captured_at`, `capture_device`; Migration `20261014_0049`). Altbelege bleiben
  ohne Angabe.
- `POST /api/v1/documents` und `POST /api/v1/receipt-ocr/suggestions` sowie die
  MCP-Tools `upload_document`/`create_receipt_ocr_suggestion` nehmen diese Angaben und
  den auf dem Gerät berechneten `client_sha256` an. Weicht der Hash ab, wird nichts
  gespeichert (422).
- Ein erneut gesendeter Beleg mit gleichem Inhalt erzeugt kein Duplikat: Der Upload
  liefert den vorhandenen Beleg mit `duplicate: true`, der OCR-Vorschlag antwortet
  mit 409.
- Belegliste zeigt den Erfassungsweg. Audit-Log und Prüferexport enthalten die
  Erfassungsangaben. Die Verfahrensdokumentation hat einen neuen Abschnitt
  „Mobile Erfassung“ (inkl. Hinweis zum ersetzenden Scannen).

### Push-Relay
- Neu: eigenständiger Dienst `push_relay/` mit eigenem Image (`push_relay/Dockerfile`)
  und `docker-compose.push-relay.yml`. Er nimmt Mitteilungen der Instanzen über
  Protokoll v1 an und stellt sie per HTTP/2 an APNs zu (ES256-Provider-Token aus dem
  `.p8`-Schlüssel, Erneuerung nach 50 Minuten, einmalige Wiederholung bei abgelaufenem
  Token). Apples `410 Unregistered` meldet er als `invalid_token`.
- Instanzen weisen sich mit einem Schlüssel aus, von dem das Relay nur den SHA-256-Hash
  kennt (`python -m push_relay new-instance-key <name>`). Rate-Limit je Instanz,
  Prüfung jedes Eintrags (Token, Umgebung, Längen, 4-KB-Grenze von APNs).
- Das Relay speichert nichts und protokolliert keine Inhalte.
- Trockenlauf `RELAY_APNS_MODE=dry-run` für den Betrieb vor dem Apple-Account.
- Neue Variablen (nur Relay-Host): `RELAY_INSTANCE_KEYS`, `RELAY_APNS_MODE`,
  `RELAY_APNS_KEY_PATH`, `RELAY_APNS_KEY_ID`, `RELAY_APNS_TEAM_ID`, `RELAY_APNS_TOPIC`,
  `RELAY_RATE_LIMIT_PER_MINUTE`, `RELAY_PORT`. Für die Relay-Tests ist `httpx[http2]`
  neu in `requirements-dev.txt`.

### Push-Mitteilungen für die iOS-App (Instanz-Seite)
- Neu: Mobilgeräte registrieren sich für Push-Mitteilungen
  (`POST /api/v1/devices`, nur mit Benutzer-Token) und wählen Ereignisse: neue
  Bankumsätze (Datei-Import, FinTS, Open Banking), ELSTER-Übermittlung erfolgreich oder
  fehlgeschlagen, tägliche Zusammenfassung überfälliger offener Posten.
- Ereignisse legen Mitteilungen in einer Ausgangswarteschlange an, und zwar in derselben
  Transaktion wie die auslösende Änderung (Migration `20261013_0048`, Tabellen
  `push_device` und `push_notification`). Der Dispatcher `flask push-dispatch`
  (Compose-Dienst `push`, Profil `push`) übergibt sie an ein zentrales Push-Relay
  (Protokoll v1, siehe README). Er versucht bis zu fünfmal, verwirft Einträge nach
  24 Stunden und widerruft abgemeldete Geräte automatisch.
- Texte nennen nur Anzahlen bzw. den Meldezeitraum, keine Beträge oder Namen.
- Geräte verwalten in UI (**Apps**: Ereignisse, Testmitteilung, Widerruf), REST und
  MCP (`get_push_status`, `list_push_devices`, `update_push_device`,
  `revoke_push_device`, `send_push_test`); im KI-Chat gesperrt.
- Neue Variablen: `PUSH_RELAY_URL`, `PUSH_RELAY_TOKEN`, `PUSH_DIGEST_HOUR`,
  `PUSH_DISPATCH_INTERVAL_SECONDS`. Ohne `PUSH_RELAY_URL` bleibt alles aus.
- Behoben: Die Liste der verbundenen Apps zeigt bei nativen Apps das App-Schema statt
  eines leeren Weiterleitungsziels.

### Server-Vorarbeit für die iOS-App (ADR-003)
- Neu: `GET /api/v1/dashboard` und MCP `get_dashboard` liefern die Übersicht für den
  Startbildschirm und die Widgets der App: offene Aufgaben, Bankstände je Bankkonto
  (Buchsaldo, Auszugssumme, offene Umsätze, letzter Buchungstag), Ergebnis des
  laufenden Geschäftsjahres bis `as_of` und Bilanzsumme. Die Dashboard-Seite nutzt
  denselben Service (`app/services/dashboard.py`).
- Neu: Der OAuth-Server akzeptiert Redirects nativer Apps über ein eigenes URI-Schema in
  Reverse-Domain-Form (RFC 8252, z. B. `de.openbuchhaltung.app:/oauth/callback`).
  Die Zustimmungsseite nennt die App, die CSP erlaubt das Schema als `form-action`.
  `OAUTH_ALLOWED_REDIRECT_HOSTS` beschränkt auch App-Schemata.
- Doku: ADR-003 „Native iOS-Begleit-App“ und Phase 6 im Umsetzungsplan.

### Investitionen in Kostenstellen- und Profitcenter-Bericht
- Neu: Der Controlling-Bericht zeigt je Einheit zusätzlich Anschaffungswert,
  Abschreibung und Buchwert. Bisher zählten nur Erlös- und Aufwandskonten – Einheiten,
  die (wie beim Breitbandausbau) nur auf Anlagekonten wie „Anlagen im Bau“ kontiert
  sind, standen deshalb überall auf 0,00. Anschaffungswert und Buchwert gelten zum
  Ende des Berichtszeitraums, die Abschreibung (planmäßig, außerplanmäßig,
  Restbuchwert beim Abgang aus der Anlagenbuchhaltung, Stornos eingerechnet) für den
  Zeitraum; Aufschlüsselung je Anlagekonto.
- `GET /api/v1/controlling-report` und MCP `get_controlling_report` liefern je Einheit
  `investments` (`acquisition_cost`, `depreciation`, `book_value`, `accounts`);
  der CSV-Export (`export_controlling_csv`) hat die Spalten `acquisition_cost`,
  `depreciation`, `book_value` und `total_*` angehängt.

### Tabellen nach jeder Spalte sortieren
- Neu: Ein Klick auf einen Spaltenkopf sortiert die Tabelle auf- bzw. absteigend (▲/▼).
  Die paginierten Listen – Journal, Bankumsätze, Belege, Geschäftspartner, Offene
  Posten und Kontoblatt – sortiert der Server über `?sort=<Spalte>&dir=asc|desc` über
  alle Seiten hinweg; Blättern und Filter behalten die Sortierung. Alle übrigen
  Tabellen sortieren im Browser (Datum, deutsche Beträge und Kontonummern numerisch,
  leere Werte am Ende); ein dritter Klick stellt die ursprüngliche Reihenfolge her.
  Summenzeilen und der Anfangssaldo bleiben stehen; GuV, Steuerberechnung,
  Mahnschreiben und Eingabetabellen bleiben unsortiert.
- Neu: Kontoblatt sortierbar per `GET /api/v1/accounts/<id>/ledger?sort=&dir=` und MCP
  `get_account_ledger` (`sort`: date, number, text, contra, debit, credit, balance).
  Der Saldo je Zeile bleibt der chronologisch laufende.

### Kostenstelle und Profitcenter nachträglich zuordnen
- Neu: Offene Buchungen lassen sich je Zeile um Kostenstelle und Profitcenter ergänzen
  oder davon befreien – `PATCH /api/v1/journal-entries/<id>` mit
  `lines[].cost_center_id`/`profit_center_id` (null entfernt), MCP `amend_journal_entry`
  und Auswahlfelder je Zeile auf der Buchungsdetailseite. Geprüft wird wie beim Buchen
  (Einheitentyp, aktiv, gültig am Buchungsdatum); Festgeschriebene, stornierte und
  Vortragsbuchungen bleiben gesperrt. Das Audit-Log (`amended`) hält alte und neue
  Einheit je Zeile fest (`cost_centers`/`profit_centers`), das Änderungsprotokoll der
  Detailseite zeigt sie an.

### Kontoblatt: Buchungen je Konto
- Neu: Kontoblatt unter `/konten/<id>` – Klick auf ein Konto im Kontenplan, im Journal
  (Buchungssatz), auf der Buchungsdetailseite, in Summen-/Saldenliste, GuV und Bilanz,
  auf der Bankseite, in OPOS, Controlling und der Kontenrahmen-Prüfung öffnet die
  Buchungszeilen des Kontos mit Anfangssaldo, Gegenkonten, Steuercode, Geschäftspartner
  und laufendem Saldo (S/H). Standard ist das laufende Geschäftsjahr; wählbar sind jedes
  Geschäftsjahr, der gesamte Zeitraum oder Von/Bis. Saldovorträge erscheinen nicht als
  Zeile, sondern als Anfangssaldo; Erfolgskonten beginnen je Geschäftsjahr bei null.
  Kontobearbeitung und Änderungshistorie sind auf der Seite enthalten.
- Neu: `GET /api/v1/accounts/<id>/ledger` (`fiscal_year_id`, `date_from`, `date_to`,
  `limit` bis 1000, `offset`) und MCP-Tool `get_account_ledger`.
- Geändert: Summen-/Saldenliste, GuV und Bilanz liefern je Zeile `account_id` (API, MCP)
  als Sprungziel fürs Kontoblatt.

### Buchungen zu Kostenstellen und Profitcentern finden
- Neu: Journalfilter nach Konto, Kostenstelle und Profitcenter in der Buchungsliste,
  `GET /api/v1/journal-entries` (`account_id`, `cost_center_id`, `profit_center_id`)
  und MCP `list_journal_entries`. Konto und Einheit müssen auf derselben Zeile stehen;
  `none` findet Erlös-/Aufwandszeilen ohne Einheit („Nicht zugeordnet“).
- Neu: Kostenstellenrechnung und Profitcenter-GuV verlinken Einheit und Konto jeder
  Zeile auf die gefilterte Buchungsliste; die Controlling-Seite hat einen
  Berichtszeitraum, der auch für Drilldown und CSV-Export gilt. „Nicht zugeordnet“
  zeigt jetzt ebenfalls die Konten.
- Korrigiert: Die MCP-Beschreibung von `get_controlling_report` versprach einen
  Buchungsdrilldown, den der Bericht selbst nicht liefert; sie verweist nun auf
  `list_journal_entries`.

### Übersichtlichere Oberfläche und Geschäftspartner an jeder Buchung
- Geändert: Geschäftspartner sind auf allen Buchungszeilen zulässig (ADR-002, Nachtrag
  2026-10-12). Auf Debitoren-/Kreditoren-Sammelkonten bildet der Partner weiter das
  Nebenbuch (Rollenprüfung, Saldovortrag je Partner), auf allen anderen Konten ist er
  Partnerbezug ohne Rollenbindung – z. B. der Lieferant einer Direktbuchung „Aufwand an
  Bank“. Der Saldovortrag trennt nur Sammelkonten nach Partner. Kein Schema- und kein
  Hashwechsel (der Partner je Zeile ist seit Hashversion 3 versiegelt).
- Neu: Partner der Buchung (`partner_id`/`partner_number` auf oberster Ebene) beim
  Buchen (`POST /api/v1/journal-entries`, Buchungsmaske), Ergänzen (`PATCH
  /api/v1/journal-entries/<id>`) und Verbuchen von Bankumsätzen (`POST
  /api/v1/bank-transactions/<id>/book`, Buchungsdialog der Bankseite): gilt für alle
  Zeilen ohne eigenen Partner außer Steuerzeilen. `GET /api/v1/journal-entries` filtert
  nach `partner_id` und liefert je Buchung `partners`; MCP-Tools `create_journal_entry`,
  `amend_journal_entry`, `list_journal_entries` und `book_bank_transaction` entsprechend
  erweitert. Vorlagen prüfen die Partnerrolle nur noch auf Sammelkonten.
- Neu: Journal mit einer Zeile je Buchung – Geschäftspartner als eigene Spalte,
  Kontierung „Soll an Haben“, Betrag, Gegenseite laut Bank als Hinweis; Filter nach
  Partner und Status; „Partner zuordnen“ ordnet offenen Buchungen direkt aus der Liste
  einen Partner zu. Die Erfassungsmaske ist eingeklappt, kennt den Partner der Buchung
  und nimmt Beträge auch im deutschen Format (1.190,00) an.
- Neu: Detailseite je Buchung (`/buchungen/<id>`, auch unter `/ergaenzen`) mit Zeilen,
  Partner je Zeile („Partner für alle Zeilen übernehmen“), Leistungsdatum, Belegen,
  Bankumsatz, Änderungsprotokoll, Festschreiben und Stornieren.
- Neu: Partnerseite mit Übersicht, Buchungen des Partners und lesbarer Änderungshistorie;
  Partnerliste mit Rollen und Anzahl der Buchungen (Link ins gefilterte Journal).
- Geändert: Oberfläche aufgeräumt – Kopfzeile in einer Zeile mit Benutzermenü, deutsche
  Beträge und Datumsangaben, Seitenkopf mit Aktionen, eingeklappte Formulare und
  Werkzeuge, Dialoge statt Formularstapeln in Tabellenzeilen. Bank: Umsätze zuerst,
  Buchen im Dialog inklusive Partner und Buchungstext; Konten mit Suche und Filter;
  Belege mit Filter „nur ohne Buchung“; OPOS mit Überfälligkeits-Kennzeichen; UStVA mit
  eingeklappter ELSTER-Konfiguration; Dashboard, Anlagen und Berichte im deutschen
  Zahlenformat.

### Belegkategorien und mehrere Belege je Buchung
- Neu: Belegkategorien je Gesellschaft mit der Vorauswahl Rechnung, Gutschrift,
  Zahlungsbeleg, Bewirtungsbeleg und Zahlungsanweisung (Migration `20261012_0047` legt sie
  für bestehende Gesellschaften an, neue Gesellschaften erhalten sie beim Anlegen).
  Admin und Buchhalter können eigene Kategorien ergänzen und Kategorien deaktivieren;
  bestehende Belege behalten deaktivierte Kategorien. Kategorie beim Upload und
  nachträglich wählbar, jede Zuordnung im Audit-Log. E-Rechnungs-Importe erhalten
  automatisch „Rechnung“.
- Neu: API `GET/POST /api/v1/document-categories`,
  `POST /api/v1/document-categories/<id>/active`, `POST /api/v1/documents/<id>/category`,
  `category_id` bei Upload und als Filter der Belegliste; MCP-Tools
  `list_document_categories`, `create_document_category`, `set_document_category_active`,
  `set_document_category`.
- Neu: Die Buchungsliste zeigt alle Belege einer Buchung mit Kategorie und Download-Link;
  „+ weiterer Beleg“ öffnet den Upload mit vorausgewählter Buchung. `GET
  /api/v1/journal-entries` liefert je Buchung die verknüpften Belege.
- Geändert: Der Belegabgleich berücksichtigt auch Buchungen, die schon Belege haben
  (nachrangig nach Buchungen ohne Beleg). Passen nur solche Buchungen, schlägt die
  Regel den Beleg als weiteren Nachweis zur nächstgelegenen Buchung vor (höchstens
  60 Tage Abstand zum Belegdatum); das LLM sieht die Anzahl vorhandener Belege.
- Neu: Der Prüferexport enthält den Datensatz `document_categories`; der Belegindex führt
  `category_id`.

### Weitere Domains für die öffentliche Instanz
- Neu: `PUBLIC_DOMAIN_ALIASES` im nginx-Overlay macht dieselbe Instanz unter weiteren
  Domains erreichbar, ohne Umleitung und je Domain mit eigenem Let's-Encrypt-Zertifikat
  (nginx wählt es per SNI, certbot holt und erneuert es, der Reload-Hook überwacht es).
- Der OAuth-Hinweis des MCP-Servers im 401 folgt der aufgerufenen Domain statt fest
  `PUBLIC_DOMAIN` (`MCP_PUBLIC_URL` bleibt als Override).
- Fix: certbot gibt Zertifikate und Schlüssel für die Gruppe des nginx-Workers frei
  (nach Platzhaltern, Ausstellen und jeder Erneuerung). Per SNI gewählte Zertifikate
  lädt der Worker statt des root-Masters; ohne Leserecht scheiterte der TLS-Handshake.

### Projekt-Webseite
- Neu: statische Projekt-Webseite unter `website/` für openbuchhaltung.de: Funktionen,
  Ablauf vom Beleg bis zur Meldung, Aufnahmen der Demo-Instanz, Umsatzsteuer und DATEV,
  Prüfbarkeit (GoBD-Basis), REST-API/MCP/KI-Chat, Betrieb, Installation, Projektstand und
  FAQ sowie Impressum, Datenschutz und 404-Seite. Ohne Cookies, Tracking und Inhalte von
  Drittanbietern, ohne Inline-Skripte und -Styles; helles und dunkles Farbschema.
- Neu: Overlay `docker-compose.website.yml` liefert die Seite über den nginx des
  nginx-Overlays unter `WEBSITE_DOMAIN` aus: eigenes Let's-Encrypt-Zertifikat für Domain
  und `www` (Umleitung auf die Domain), strenge Content-Security-Policy, keine
  Zugriffsprotokolle. Die Dateien kommen per Bind-Mount aus dem Checkout.
- certbot-Service und Reload-Hook des nginx-Overlays verwalten dafür ein zweites
  Zertifikat; ohne `WEBSITE_DOMAIN` bleibt alles wie bisher. Das nginx-Overlay bindet
  seine Vorlage jetzt als einzelne Datei ein.
- `tests/test_website.py` prüft Links, lokale Ressourcen, CSP-Tauglichkeit der Seiten und
  die nginx-Vorlage.

### Open Banking (PSD2) für Banken ohne FinTS
- Neu: Umsatzabruf über Enable Banking für Banken ohne FinTS, z. B. Finom.
  Bankzugang anlegen (Bank laut Enable-Banking-Liste, Geschäfts-/Privatkonto,
  Bank-Sachkonto, optional IBAN), Bank-Zustimmung per Weiterleitung zur Bank
  (Rücksprung `/bank/open-banking/callback`), Abruf gebuchter Umsätze mit Pagination
  über die gemeinsame Import-Pipeline (Quelle `open_banking`, Bankreferenz =
  `entry_reference`), Deaktivieren widerruft die Sitzung.
- Tabelle `open_banking_connection` (Migration 0046); die Sitzungs-ID wird
  Fernet-verschlüsselt (Schlüssel aus `SECRET_KEY`) gespeichert.
- API: `GET /api/v1/open-banking/institutes`, `GET/POST /api/v1/open-banking-connections`,
  `…/<id>/active`, `…/<id>/authorize`, `…/<id>/sync`, `POST /api/v1/open-banking/complete`.
  MCP: `list_open_banking_institutes`, `list_open_banking_connections`,
  `create_open_banking_connection`, `set_open_banking_connection_active`,
  `start_open_banking_authorization`, `complete_open_banking_authorization`,
  `sync_open_banking_transactions` (im KI-Chat gesperrt).
- Konfiguration: `ENABLE_BANKING_APP_ID`, `ENABLE_BANKING_KEY_FILE`,
  `ENABLE_BANKING_REDIRECT_URL`, `ENABLE_BANKING_API_URL`.

### Bankimport: Finom-Kontoauszug
- Der CSV-Import erkennt den Original-Export der Finom-Bank (Spalten
  „Transaktions-ID“ und „Zahlungsbetrag“) – in UI, API und MCP
  `import_bank_transactions`. Übernommen werden nur Zeilen mit Status „Completed“;
  Betrag = Zahlungsbetrag, Gegenpartei = Auftraggeber/Empfänger, Kartenzahlungen als
  „Kartenzahlung {Empfänger}“ plus „({Währung} {Ursprungsbetrag})“ bei Fremdwährung.
  Die Transaktions-ID wird bankseitige Referenz.
- Bestände aus der bisherigen manuellen Umwandlung (ohne Referenz) gelten beim
  Re-Import als Duplikat – jetzt auch mehrere gleich aussehende Zeilen derselben Datei
  (Hash-Suffix `|n:k`), nicht nur die erste.
- Das CSV-Trennzeichen wird aus der Kopfzeile bestimmt, damit Semikolons in
  Verwendungszwecken komma-getrennte Dateien nicht fehldeuten.

### FinTS-Registrierung und Bankenliste
- `FINTS_PRODUCT_ID` ist die 25-stellige Registrierungsnummer der Deutschen
  Kreditwirtschaft: Beim Start wird eine abweichende Länge gewarnt, beim Abruf werden
  mehr als 25 Zeichen oder Leerzeichen abgelehnt. Doku: je Betreiber eigene
  Registrierung, Nummer nur in die Instanz-`.env`.
- Neu: FinTS-Bankenliste (`fints_institute`, Migration 0045) – Import der DK-CSV
  (cp1252/UTF-8, ersetzt den Bestand) durch globale Administratoren in der Verwaltung,
  per `POST /api/v1/fints-institutes/import`, MCP `import_fints_institutes` und
  `flask import-fints-institute`. Die Liste wird nicht mitgeliefert.
- BLZ-Abfrage `GET /api/v1/fints-institutes?blz=…` bzw. MCP `lookup_fints_institute`;
  im Anlageformular schlägt die BLZ Institut, BIC und FinTS-URL vor. Die FinTS-URL ist
  beim Anlegen optional und wird sonst aus der Liste ermittelt.
- FinTS-Fehler zeigen die Rückmeldungen der Bank (Code und Text aus HIRMG/HIRMS) in
  Fehlermeldung und Log. python-fints meldet abgelehnte Anmeldungen pauschal als
  „PIN wrong?“ und loggt die Rückmeldungen dabei nicht; bei diesem Fehler weist die
  Meldung zusätzlich auf die Sperre nach mehreren Fehlversuchen hin.
- Sicherheit (Review S5): Mit importierter Liste sind nur FinTS-URLs aus der Liste
  zulässig, beim Anlegen und vor jedem Abruf. Hosts im lokalen oder privaten Netz und
  URLs mit Zugangsdaten sind immer gesperrt.

### Leistungsdatum und Ergänzen offener Buchungen
- Neu: Buchungen tragen optional ein Leistungsdatum (`journal_entry.service_date`,
  Migration 0044) – Feld in der Buchungsmaske, `service_date` in
  `POST /api/v1/journal-entries` und MCP `create_journal_entry`, Anzeige im Journal und
  Spalte im Journal-CSV.
- UStVA, Jahreserklärung und ZM ordnen danach zu: Umsätze und EU-Leistungen (Kz 21,
  ZM „S“, § 13b Abs. 1 Kz 46/47) im Zeitraum der Leistung, ig. Lieferungen, ig. Erwerb
  und übrige § 13b-Fälle mit der Rechnung (spätestens Ende des Folgemonats), Vorsteuer
  zum späteren Datum aus Leistung und Rechnung. Ohne Leistungsdatum gilt weiter das
  Buchungsdatum. Ein Storno übernimmt das Leistungsdatum; in der ZM heben sich
  Buchung und Storno ohne Partner im selben Zeitraum auf (nicht „fehlend“).
- Neu: Offene (nicht festgeschriebene) Buchungen ergänzen – Leistungsdatum und
  Geschäftspartner auf Sammelkonto-Zeilen; UI-Link „Ergänzen“, API
  `PATCH /api/v1/journal-entries/<id>`, MCP `amend_journal_entry`. Jede Änderung steht
  mit altem und neuem Wert im Audit-Log; festgeschriebene und stornierte Buchungen,
  gesperrte Perioden und abgeschlossene Geschäftsjahre sind ausgeschlossen.
- Inhaltshash Version 4 versiegelt das Leistungsdatum; Siegel der Versionen 2 und 3
  bleiben gültig (kein Re-Hashing).
- DATEV-Export: Mit Leistungsdatum schreibt der Stapel 116 Felder mit Feld 115
  „Leistungsdatum“ und Feld 116 „Datum Zuord. Steuerperiode“; sonst unverändert 14.
- Über die API angelegte Buchungen protokollieren jetzt den API-Benutzer statt
  „system“.

### UStVA: EU-Umsätze, Reverse Charge, ig. Erwerb und Zusammenfassende Meldung

- Erträge ohne Umsatzsteuer ordnet die UStVA über die DATEV-Kontenfunktion des
  Erlöskontos zu: Kz 41 (ig. Lieferung, 8125/4125), 42 (Dreiecksgeschäft), 43 (Ausfuhr,
  § 4 Nr. 2–7), 44, 48 (§ 4 Nr. 8 ff.), 21 (sonstige Leistung an EU-Unternehmer,
  8336/4336), 45 (nicht steuerbar), 60 (§ 13b als Leistender), 87 (0 %). Neue Spalte
  `kennzahl` in `data/kontenrahmen/datev_kontenfunktionen.csv`.
- **Geändert:** Erträge ohne Umsatzsteuer auf Konten ohne UStVA-Funktion (z. B. Zinsen)
  zählen nicht mehr pauschal in Kz 48 (Review F10), sondern erscheinen als Hinweis;
  `GET /api/v1/vat-return` liefert dazu `warnings` und `unassigned`, die UStVA-Seite
  zeigt beides.
- Reverse Charge als Leistungsempfänger und innergemeinschaftlicher Erwerb über die
  Steuerkonten (SKR03 1572/1574, 1577/1578, 1772/1774, 1785/1787; SKR04 1402/1404,
  1407/1408, 3802/3804, 3835/3837): Kz 46/47 bzw. 84/85, 89/93, Vorsteuer 61/67;
  Bemessungsgrundlage aus den Aufwands-/Anlagenzeilen der Buchung (ohne Aufwandszeile
  oder bei zusätzlicher normaler Vorsteuer aus Steuer ÷ Satz), EU-Leistungen über das
  EU-Aufwandskonto (3123/5923) oder einen Partner mit EU-USt-IdNr.; Kz 83 rechnet alle
  Umsatz- gegen alle Vorsteuer.
- Neu: Zusammenfassende Meldung je Kunden-USt-IdNr. und Art (L/D/S) – UI auf der
  UStVA-Seite, API `GET /api/v1/zm`, MCP `get_zm_report`.
- DATEV-Kontenfunktionen liegen jetzt in `app/services/datev_account_functions.py`
  (gemeinsam für DATEV-Export und UStVA).

### DATEV-Export: Rundungscent und Steuerkonten des anderen Kontenrahmens

- Weicht die gebuchte Steuer um wenige Cent von DATEVs Rechnung aus dem Bruttobetrag ab
  (einzeln gerundete Rechnungspositionen, Aufteilung auf mehrere Gegenkonten; höchstens
  5 Cent und 1 % der Steuer), bleibt der Satz brutto mit Automatikkonto bzw.
  Steuerschlüssel; ein Korrektursatz „Steuer-Rundungsdifferenz“ verschiebt die Differenz
  zwischen Konto und DATEV-Steuerkonto. Bisher fiel der ganze Satz auf Netto plus
  eigene Steuerzeile zurück.
- Steuerkonten des jeweils anderen Kontenrahmens (SKR04 1401/1406/3801/3806 in einer
  SKR03-Buchhaltung und umgekehrt) erscheinen im Stapel unter der Nummer des erkannten
  Rahmens; Sätze, die danach Konto und Gegenkonto gleich hätten, entfallen.

### DATEV-Export je Wirtschaftsjahr

- Der Buchungsstapel umfasst genau ein Wirtschaftsjahr bzw. einen Zeitraum darin:
  DATEV liest das Jahr des Belegdatums (TTMM) aus dem WJ-Beginn im Kopffeld 13.
  Bisher enthielt die Datei alle Jahre mit WJ-Beginn 1.1. des ältesten Belegs; ein
  Mehrjahresstapel landete in DATEV im falschen Jahr oder wurde abgelehnt (Review F3).
- Kopffeld 13 ist der tatsächliche WJ-Beginn (auch abweichendes und Rumpf-WJ),
  15/16 der gewählte Zeitraum (Standard: das ganze Wirtschaftsjahr), das
  Festschreibekennzeichen (Feld 21) gilt nur für die exportierten Buchungen.
- **Geändert:** `GET /api/v1/exports/datev.csv` und MCP `export_datev_csv` nehmen
  `fiscal_year_id` sowie optional `date_from`/`date_to`. Ohne Angabe wird das
  Wirtschaftsjahr mit Buchungen exportiert; haben mehrere Wirtschaftsjahre
  Buchungen, antwortet die API mit 400 und listet sie unter `fiscal_years`.
  Ein Zeitraum über die WJ-Grenze ergibt ebenfalls 400. Dateiname mit WJ und
  Zeitraum (`EXTF_Buchungsstapel_1_WJ2026_20260101-20261231.csv`).
- UI: Auf der Berichte-Seite Auswahl des Wirtschaftsjahres (vorgewählt das des
  Auswertungszeitraums bzw. das jüngste mit Buchungen) mit optionalem Von/Bis;
  eine ungültige Auswahl erscheint als Hinweis (`GET /reports/datev.csv`).

### DATEV-Export: Brutto-Prinzip und Gegenkonto

- Der Buchungsstapel folgt dem Brutto-Prinzip, das DATEV beim Import verlangt
  (DATEV-Hilfe Dok.-Nr. 1036228): Steuerzeilen gehen in den Bruttobetrag ihrer
  Bemessungsgrundlage auf – auf Automatikkonten (AM/AV laut DATEV-Kontenrahmen 2026,
  z. B. 8400/4400, SKR03 3400) ohne BU-Schlüssel, sonst mit Steuerschlüssel
  101/102/401/402. Bisher standen der Nettobetrag auf dem Automatikkonto und die Steuer
  in eigener Zeile; DATEV hätte die Steuer ein zweites Mal errechnet.
- Rechnet DATEV aus dem Brutto nicht exakt die gebuchte Steuer (Rundung laut Rechnung,
  Aufteilung auf mehrere Gegenkonten), passt der Steuersatz nicht zur Kontenfunktion oder
  fehlt die Steuerzeile (Abschluss-, Umbuchungen), trägt der Satz auf dem Automatikkonto
  BU 40 (Aufhebung der Automatik) und die Steuer bleibt eigener Satz.
- Jeder Buchungssatz hat Konto und Gegenkonto (DATEV-Muss-Felder, sonst Importfehler
  REW00223); mehrzeilige Buchungen werden zerlegt statt als Zeilen ohne Gegenkonto
  exportiert.
- Der Kontenrahmen SKR03/SKR04 wird erkannt (`detect_company_chart`), im Kopffeld 27
  „Sachkontenrahmen“ übergeben und auf der Berichte-Seite am Download genannt; die
  Kopfzeile hat alle 31 Felder des DATEV-Musters.
- Neu: `data/kontenrahmen/datev_kontenfunktionen.csv` (Automatik- und Zusatzfunktionen
  aus den DATEV-Kontenrahmen 2026) und `tools/datev_kontenfunktionen.py` zum Erneuern.
- MCP: API-Antworten werden im Zeichensatz des Content-Type gelesen; `export_datev_csv`
  liefert Umlaute damit korrekt (vorher Ersatzzeichen, weil Windows-1252 als UTF-8 galt).

### UStVA: Steuer auch ohne Steuercodes

- UStVA und DATEV-Export erkennen Steuerzeilen über dieselbe Funktion
  (`company_tax_accounts`): die Steuerkonten der Steuercodes und – auch wenn eine
  Gesellschaft gar keine Steuercodes hat – die Standard-Steuerkonten 1571/1576/1771/1776
  (SKR03) bzw. 1401/1406/3801/3806 (SKR04), sofern Kontoart und Bezeichnung passen.
  Bisher zählte die UStVA ohne Steuercodes alle Erlöse als steuerfrei (Kz 48) und
  keine Vorsteuer (betraf z. B. Buchungen auf alten SKR04-Steuerkonten in einer
  SKR03-Buchhaltung).
- Umbuchungen zwischen Steuerkonten (Bereinigung 3806 → 1776, 1406 → 1576) sind für die
  UStVA neutral; der Hinweis der Kontenrahmen-Prüfung zu Steuerkonten mit Steuercode
  nennt deshalb nur noch das Deaktivieren als Problem.

### KI-Zugang (LLM-API-Key) je Benutzer

- Der Administrator hinterlegt je Benutzer einen KI-Zugang: Standard OpenAI
  (`https://api.openai.com/v1/responses`, nur API-Key und optional Modell) oder ein
  anderer OpenAI-`/responses`-kompatibler Endpunkt (Azure, OpenRouter, Ollama …;
  Key optional). Der Key liegt Fernet-verschlüsselt (Schlüssel aus `SECRET_KEY`
  abgeleitet) in `user.llm_api_key_encrypted`, ausgegeben werden nur die letzten
  vier Zeichen; Audit-Ereignisse `llm_settings_updated`/`llm_settings_cleared`.
- KI-Chat, Beleg-OCR, KI-Kontrolle, Belegabgleich und Dokument-Update verwenden den
  Zugang des handelnden Benutzers und senden den Key als Bearer-Header; ohne Zugang
  gelten die Instanz-Endpoints (`*_LLM_ENDPOINT_URL`) wie bisher. PDF-Scans gehen
  als `input_file` an den OCR-Endpoint (von OpenAI akzeptiertes Format).
- UI: Abschnitt **KI-Zugang (LLM) je Benutzer** in der Verwaltung; API:
  `POST /api/v1/users/<id>/llm`, `POST /api/v1/users/<id>/llm/delete`, Feld `llm`
  in der Benutzerausgabe; MCP: `set_user_llm_settings`, `clear_user_llm_settings`
  (im KI-Chat gesperrt). Migration `20261009_0043`.
- **Entfernt:** `CHAT_LLM_API_KEY` wird nicht mehr ausgewertet (Warnung beim Start).
- Neue Abhängigkeit `cryptography`.

### Geschäftspartner (Debitoren/Kreditoren), PR 1

- Neuer Kunden-/Lieferantenstamm `business_partner`: Debitoren- (10000–69999) und
  Kreditorennummern (70000–99999) mit automatischer Vergabe, Art, Anschrift,
  USt-IdNr, Steuernummer, Kontakt, Zahlungsziel, Notizen, Aktivstatus. Nummern und
  Rollen sind nach der ersten Verwendung fest; kein Löschen, nur Deaktivieren;
  Dublettenhinweise (USt-IdNr, IBAN, Name). Vorher-/Nachher-Historie, Bankdaten
  nur über eigenen Endpunkt (`bank_details_changed`), im KI-Chat gesperrt.
- Nebenbuch statt Personenkonten (ADR-002): Kontenkennzeichen `subledger`
  (`debtor`/`creditor`), `partner_id` an Buchungszeilen nur auf passenden
  Sammelkonten. Festschreibungs-Hash Version 3 inkl. Partner; Version-2-Siegel
  bleiben unverändert gültig. Storno, Buchungsvorlagen, Eröffnungsbilanz
  (vierte Spalte Partnernummer) und Saldovortrag (je Partner) übernehmen den
  Partner; Journal-API und Journal-CSV zeigen Partnernummer und -name.
- UI: Seite **Partner** (Liste, Anlage, Detail mit Bankdaten und Historie),
  Partnerauswahl je Buchungszeile, Sammelkonto-Spalte unter **Konten**.
  API: `/api/v1/partners` (+ `/<id>`, `/<id>/bank-details`, `/<id>/history`);
  MCP: `list_partners`, `get_partner`, `create_partner`, `update_partner`,
  `set_partner_bank_details`, `get_partner_history`. Prüferexport:
  `business_partners.json`, `business_partner_history.json`.
- Kontoarten werden auch bei der Kontoanlage (UI/API/MCP) geprüft; `receivable`/
  `payable` sind keine gültigen Kontoarten mehr (Bilanz und Saldovortrag werten
  sie nicht aus) und erscheinen in der Kontenrahmen-Prüfung als unbekannte
  Kontoart. Ungültige Altwerte (z. B. 4240 „Strom“ aus dem alten SKR04-Import)
  lassen sich per UI/API/MCP reparieren. Der Kontenrahmen-Import kennzeichnet
  Forderungen/Verbindlichkeiten aLuL an der exakten Bezeichnung als Sammelkonto
  oder nimmt eine optionale Spalte `subledger`.
- Sammelkonten erscheinen nicht mehr in der Bankkontenauswahl.
- Migration `20261008_0042` (keine Datenänderung an Bestandskonten:
  Sammelkonten bitte unter **Konten** kennzeichnen).

### Kontenrahmen SKR04

- `data/kontenrahmen/skr04.csv` neu aufgestellt nach DATEV-Kontenrahmen SKR04 2026
  (Art.-Nr. 11175), Umfang wie `skr03.csv` (30 Konten — „Aufwendungen für bezogene
  Leistungen“ ist im SKR04 die GuV-Position von 5900 Fremdleistungen): u. a. Kasse 1600,
  Bank 1800, Geldtransit 1460, Forderungen aLuL 1200, Verbindlichkeiten aLuL 3300,
  Erlöse 19 %/7 % 4400/4300, Aufwendungen 5100–6930. Die alte Datei mischte SKR03-Nummern
  (1000 Kasse, 1200 Bank, 1600 Verbindlichkeiten, 8000/8300 Erlöse …) mit
  SKR04-Steuerkonten; „Gas, Strom, Wasser“ landete wegen eines unmaskierten Kommas als
  Konto „Gas“ mit Kontoart „Strom“.
- Funktionskonten kontenrahmensicher (`app/services/standard_accounts.py`): Geldtransit
  nur über die Bezeichnung (SKR04 1360 = Darlehen, SKR03 1460 = Zweifelhafte
  Forderungen), Gewinnvortrag 0860/2970 per Nummer nur mit Kontoart `equity` (SKR04 0860
  = Beteiligungen an Personengesellschaften), Saldenvortrag bevorzugt 9000. Bank-,
  Beleg-OCR-, Belegabgleich- und E-Rechnungsmaske wählen Bank- bzw. Kreditorenkonto
  passend zum Kontenrahmen vor statt fest 1200/1600 (im SKR04 Forderungen bzw. Kasse).
- Kontenrahmen-Import lehnt Zeilen mit mehr Spalten als die Kopfzeile und unbekannte
  Kontoarten ab; die Kontoart wird kleingeschrieben übernommen.
- Neu: Kontenrahmen-Prüfung — UI-Hinweis auf **Konten**, `GET /api/v1/account-chart/check`,
  MCP-Tool `check_account_chart`. Meldet Altkonten aus dem fehlerhaften SKR04-Import mit
  richtiger SKR04-Nummer, Buchungsanzahl und Saldo sowie Konten mit unbekannter Kontoart.
  Bestehende Gesellschaften werden nicht automatisch umgebaut (Kontonummern bleiben
  unveränderlich, GoBD); Vorgehen im README unter „Kontenrahmenimport“.

### OAuth für MCP-Connectoren

- OAuth-2.1-Server nach MCP-Autorisierungsspezifikation, damit ChatGPT- und
  claude.ai-Connectoren sich anmelden können (beide senden keine festen Bearer-Tokens):
  Discovery (RFC 9728/8414), Dynamic Client Registration (RFC 7591),
  Authorization-Code-Flow mit PKCE S256, Login und Zustimmungsseite, Refresh-Token-Rotation,
  Widerruf (RFC 7009). Access-Tokens wirken wie Benutzer-Tokens (Rolle, Mandant, Audit).
- MCP-HTTP antwortet bei 401 mit `WWW-Authenticate: Bearer resource_metadata=…`.
- Verbundene Apps: UI-Seite **Apps**, API `GET /api/v1/oauth/grants`,
  `POST /api/v1/oauth/grants/<id>/revoke`, MCP-Tools `list_oauth_grants`,
  `revoke_oauth_grant`. Migration `20261008_0041`.

### MCP

- MCP-HTTP-Endpunkt akzeptiert Benutzer-API-Tokens (`MCP_HTTP_ALLOW_USER_TOKENS`,
  Default an): Prüfung über `GET /api/v1/users/me`, Weitergabe pro Request an die
  REST-API — Tools laufen mit Rolle, Mandant und Audit-Akteur des Benutzers.
- Neuer Endpunkt `GET /api/v1/users/me` und MCP-Tool `get_current_user`.

### Betrieb

- Overlay `docker-compose.nginx.yml`: nginx-Reverse-Proxy mit Let's-Encrypt-Zertifikat
  (certbot, HTTP-01) für eine öffentlich erreichbare Instanz — Web-UI, REST-API und
  `/mcp` unter einer Domain (`PUBLIC_DOMAIN`), Login-Drossel, Platzhalter-Zertifikat
  bis zur ersten Ausstellung, automatische Erneuerung und nginx-Reload.
- `redeploy.sh`/`backup.sh` lesen `COMPOSE_FILE` auch aus der `.env` und akzeptieren
  mehrere Compose-Dateien (`:`-getrennt); Host-Port des MCP-Servers per `MCP_PORT`.

### Behoben

- Kontenrahmen-Prüfung erkennt SKR03-Buchhaltung nach dem fehlerhaften SKR04-Import.
  Bisher galt jede Gesellschaft mit SKR04-Steuerkonten als SKR04-Gesellschaft; bucht
  sie faktisch SKR03, meldete die Prüfung ihre richtigen SKR03-Konten als Altkonten
  und empfahl die Umbuchung in die falsche Richtung. Jetzt bestimmt sie den
  vorherrschenden Kontenrahmen (`dominant_chart`, `chart_evidence`) an den Konten
  außerhalb des alten Imports, deren Nummernbereich nur in einem Kontenrahmen zur
  Kontoart passt, samt Buchungszeilen. Bei SKR03 meldet sie die SKR04-Fremdkonten
  0420, 1401/1406, 2100/2180, 2970 und 3801/3806 mit SKR03-Gegenkonto (0200,
  1571/1576, 1800/1890, 0860, 1771/1776 laut DATEV-Kontenrahmen SKR03 2026),
  Bedeutung der Nummer im SKR03, Buchungsanzahl, Saldo, Belegung der SKR03-Nummer
  und verweisenden Steuercodes (`foreign_skr04_accounts`; Steuerkonten mit
  Steuercode nicht umbuchen, weil die UStVA Steuerzeilen darüber erkennt). Als
  Hinweis ohne Einfluss auf `ok` kommen die Nummern der alten Datei hinzu, die im
  SKR03 ein anderes Konto bezeichnen oder kein Einzelkonto sind: 0400, 3400, 4800,
  6200 und 8000 mit Standardkonto 0010, 3100, 4260, 4830 bzw. 8400
  (`nonstandard_skr03_accounts`). Gilt für die Seite **Konten**,
  `GET /api/v1/account-chart/check` und das MCP-Tool `check_account_chart`; die
  Prüfung bleibt lesend, Vorgehen im README unter „Kontenrahmen-Prüfung und
  Altbestände“.
- Kostenstellen- und Profitcenter-Bericht zählen Erlöskonten vom Typ `income` als
  Erlös (Filter, Vorzeichen, `total_revenue`) — wie die GuV über
  `REVENUE_ACCOUNT_TYPES`. Bisher fehlten bei SKR03/SKR04-Kontenrahmen und bei in
  der UI angelegten Ertragskonten sämtliche Erlöse; betroffen waren UI,
  `GET /api/v1/controlling-report`, `/api/v1/exports/controlling.csv` und die
  MCP-Tools `get_controlling_report`/`export_controlling_csv`.

## [0.1.0] – 2026-09-20

Erstes versioniertes Release. Fasst den bis dahin auf `main` erreichten Stand
zusammen (siehe `docs/review/projektreview-2026-09-19.md` für die Bewertung).

### Betrieb (Sprint 3, PR #149)

- `redeploy.sh` arbeitet ausschließlich mit `docker-compose.production.yml`,
  zieht vor `compose down` ein Backup, gibt Commit/Tag aus, führt
  `alembic upgrade head` als eigenen Schritt aus und wartet auf den Health-Check.
- `backup.sh`: `pg_dump` (Custom-Format), Archiv der Belegablage, Metadaten
  (Commit, Alembic-Revision, Image), Prüfsummen, Aufbewahrung; Restore-Runbook in
  `docs/compliance/produktionsbetrieb.md`; systemd-Timer für Backup und
  `verify-integrity` unter `deploy/systemd/`.
- Dockerfile: Basis-Image per Digest gepinnt, unprivilegierter Benutzer `app`,
  `HEALTHCHECK` gegen `/api/v1/health`, gunicorn als `CMD`; `.dockerignore`
  hält `instance/`, `.git`, `.venv`, Tests und Agenten-Verzeichnisse aus dem Image.
- gunicorn mit `--timeout 180 --graceful-timeout 30`, Access-Log mit Dauer und
  Request-ID; `healthcheck` für den `app`-Service im Compose.
- `DB_AUTO_MIGRATE`: Auto-Migration beim Start abschaltbar (Produktion: aus,
  fail-fast bei Schema-Rückstand); `flask`-CLI-Aufrufe migrieren eine bestehende
  Datenbank nicht mehr nebenbei.
- Logging über `dictConfig` (`LOG_LEVEL`, `LOG_FORMAT=text|json`), Request-ID
  je Request (`X-Request-ID` ein- und ausgehend), Alembic-`fileConfig` ohne
  `disable_existing_loggers`.
- Health-Endpoint mit `SELECT 1`, Alembic-Revision gegen Head, Version und Commit
  (503 bei Störung).
- Engine mit `pool_pre_ping`; SQLite mit `PRAGMA foreign_keys=ON` und WAL.
- Versionsnummer in `pyproject.toml`, dieses Changelog, `.env.example` und
  ENV-Referenz im README; ELSTER-/Lohn-Runner-Optionen aus der Umgebung lesbar.
- `worker`-Platzhalter (und ungenutztes `redis`) aus `docker-compose.yml`
  entfernt; Caddyfile mit Sicherheits-Headern, dokumentierter IP-Allowlist und
  optionalem Rate-Limit (`deploy/caddy/Dockerfile`).

### Fachliche Korrektheit (Sprint 1, PR #147)

- Abschlussbuchungen (Periode 13) in SuSa/GuV/Bilanz/UStVA/KSt standardmäßig
  ausgeklammert (Schalter `include_closing_entries`), Jahresabschluss atomar mit
  Saldovortrag ins Folgejahr; Netto-aus-Brutto mit Rundungscent auf der
  Steuerzeile; Buchung und Fachoperation in einer Transaktion (AfA, Lohn,
  Belegabgleich, Storno); `TaxCode.kind` (input/output) mit Validierung;
  Buchungsnummern je Geschäftsjahr mit Sequenztabelle und Retry; Storno-Hooks
  für Bankumsatz, AfA, Lohnlauf und offene Posten; Cent-Quantisierung,
  Stornodatum ≥ Originaldatum, Prüfung überlappender Geschäftsjahre.

### Sicherheit (Sprint 2, PR #148)

- KI-Chat: nur lesende Tools sofort, schreibende mit Bestätigungsschritt,
  sensible Tools gesperrt, Anhänge und Tool-Ergebnisse als „untrusted data“,
  Secret-Redaktion vor Persistierung; `/api/v1/mcp/call` in-process mit
  Aufruferkontext; `ProxyFix` (`TRUSTED_PROXY_COUNT`), Login-Rate-Limit in der
  Tabelle `login_attempt`, Secure-Cookie/HSTS/Session-Laufzeit in Produktion,
  Open-Redirect-Schutz, Admin-Check bei Mandantenanlage, MCP-HTTP-Body-Limit,
  Audit-Events für Login/Token/Benutzer/Passwort, Passwort-ändern-Flow
  (UI/API/MCP/CLI).

### Funktionsumfang zum Release

- Kernbuchhaltung: Mandanten, Gesellschaften, Kontenrahmen (SKR03/SKR04-Import),
  Steuercodes, mehrzeilige Journalbuchungen mit Validierung, Storno,
  Festschreibung mit DB-seitigem Schutz, Buchungsvorlagen, Eröffnungsbilanz.
- Belege: Upload mit Hash und Versionierung, OCR-/LLM-gestützte
  Buchungsvorschläge, Belegabgleich, E-Rechnung (XRechnung/ZUGFeRD) Import/Export.
- Bank und OPOS: Kontoauszugs-Import (CSV, CAMT.053, MT940), FinTS-Direktabruf
  mit TAN-Flow, Bankregeln, Zahlungsabgleich, offene Posten, Mahnwesen,
  SEPA-Zahllauf (pain.001).
- Anlagenbuchhaltung (AfA, GWG, Sammelposten, Abgang) und Lohn-MVP.
- Auswertungen: Summen-/Saldenliste, GuV, Bilanz, Kostenstellen/Profitcenter,
  UStVA-/USt-Jahres-Snapshots mit ELSTER-Preflight, KSt/GewSt-Arbeitssnapshots,
  DATEV-Buchungsstapel, Prüferexport mit Manifest und Hashes.
- Schnittstellen: REST-API mit Token-Auth, MCP-Server (stdio und Streamable
  HTTP), KI-Chat mit Tool-Zugriff; Rollen, Tenant-Scoping, append-only Audit-Log
  mit Hashkette, Integritätsprüfung per UI/API/MCP/CLI.

[Unreleased]: https://github.com/unikinCK/OpenBuchhaltung/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/unikinCK/OpenBuchhaltung/releases/tag/v0.1.0
