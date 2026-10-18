# Anforderungen für GoBD- und IDW-PS-880-Readiness

Stand: 12. Juli 2026

## 1. Ziel und Einordnung

Dieses Dokument beschreibt die fachlichen, technischen und organisatorischen Anforderungen, die OpenBuchhaltung erfüllen sollte, damit die Software im Rahmen einer Jahresabschlussprüfung und einer Softwareprüfung nach IDW PS 880 belastbar beurteilt werden kann.

Wichtig:

- Es gibt keine allgemeine staatliche Zertifizierung, die eine Buchhaltungssoftware automatisch für jeden Wirtschaftsprüfer verbindlich akzeptabel macht.
- Eine Prüfung nach IDW PS 880 bezieht sich auf eine klar definierte Softwareversion, einen festgelegten Funktionsumfang und dokumentierte Kontrollen.
- Die Ordnungsmäßigkeit hängt zusätzlich vom konkreten Betrieb beim Anwender ab. Auch eine geprüfte Software ersetzt daher keine Verfahrensdokumentation, Berechtigungskontrollen, Datensicherung und ordnungsgemäße Nutzung.
- Ziel dieses Dokuments ist zunächst die Herstellung einer belastbaren Prüfungsreife, nicht die Vorwegnahme eines Prüfungsurteils.

## 2. Prüfungsgegenstand definieren

Vor einer externen Prüfung muss der Prüfungsgegenstand eindeutig festgelegt werden:

- Produktname und Versionsnummer
- Git-Commit oder signiertes Release-Tag
- Datenbankschema und Migrationsstand
- unterstützte Datenbanken
- unterstützte Betriebsmodelle, zum Beispiel Docker mit PostgreSQL
- enthaltene Module
- ausgeschlossene Funktionen
- unterstützte Rechtsformen und Kontenrahmen
- unterstützte Buchungs- und Steuerfälle
- Abhängigkeiten und Fremdkomponenten
- Konfigurationen mit Einfluss auf Ordnungsmäßigkeit und Sicherheit

Empfehlung für einen ersten Prüfungsgegenstand:

> OpenBuchhaltung 1.0, definierter Commit, PostgreSQL, Docker-Betrieb, Kernbuchhaltung, Belegverwaltung, OPOS, Anlagenbuchhaltung, Periodenabschluss, UStVA-Vorbereitung, DATEV- und Prüfexport.

## 3. Muss-Anforderungen an Buchungen

### 3.1 Vollständigkeit und Richtigkeit

Das System muss sicherstellen:

- jede Buchung ist eindeutig identifizierbar
- Soll und Haben sind betragsgleich
- Buchungsdatum, Belegdatum, Erfassungszeitpunkt und Benutzer werden gespeichert
- Konto, Gegenkonto beziehungsweise Buchungszeilen sind vollständig
- Währung und Beträge werden eindeutig und reproduzierbar verarbeitet
- Rundungen erfolgen nach dokumentierten Regeln
- Steuerbeträge sind rechnerisch nachvollziehbar
- unvollständige oder fachlich ungültige Buchungen werden abgewiesen

### 3.2 Festschreibung

Nach der Festschreibung gilt:

- Buchungen dürfen fachlich nicht mehr geändert oder gelöscht werden
- Korrekturen erfolgen ausschließlich durch Storno- oder Gegenbuchungen
- Festschreibungszeitpunkt und ausführender Benutzer werden gespeichert
- ein Festschreibelauf ist vollständig protokolliert
- doppeltes Festschreiben und Doppelstorno werden verhindert
- Stornos verweisen eindeutig auf die Ursprungsbuchung
- Stornobuchungen sind selbst festgeschrieben

### 3.3 Technischer Manipulationsschutz

Ein Anwendungsflag allein ist nicht ausreichend. Zusätzlich erforderlich:

- Datenbankregeln oder Trigger verhindern Update und Delete festgeschriebener Buchungen
- direkte Datenbankänderungen werden verhindert oder zumindest sicher erkannt
- privilegierte administrative Eingriffe werden protokolliert
- produktive Datenbankkonten folgen dem Minimalprinzip
- Anwendung, Migration und Administration verwenden getrennte Rollen
- Integritätsprüfungen erkennen nachträgliche Veränderungen

## 4. Audit-Log

Das Audit-Log muss vollständig, nachvollziehbar und manipulationsgeschützt sein.

Je Ereignis sind mindestens zu speichern:

- eindeutige Ereignis-ID
- Zeitstempel einschließlich Zeitzone
- Benutzer und Rolle
- Mandant und Gesellschaft
- Quellkanal: UI, API, MCP, Import oder Systemprozess
- Aktion
- Objektart und Objekt-ID
- vorheriger Zustand
- neuer Zustand
- fachlicher Grund, soweit relevant
- Softwareversion
- Korrelations-ID für zusammengehörige Aktionen
- Erfolg oder Ablehnungsgrund

Zusätzliche Anforderungen:

- Audit-Datensätze dürfen nicht regulär geändert oder gelöscht werden
- Audit-Ereignisse sollen über Hashes verkettet werden
- eine Integritätsprüfung muss fehlende oder veränderte Ereignisse erkennen
- sicherheits- und buchungsrelevante Fehlversuche sind ebenfalls zu protokollieren
- Exporte müssen das Audit-Log einschließlich Feldbeschreibung enthalten

## 5. Belegverwaltung und Archivierung

### 5.1 Originalerhalt

Für jeden Beleg gelten folgende Anforderungen:

- Speicherung der unveränderten Originaldatei
- Hashbildung unmittelbar bei Eingang
- Speicherung von Eingangszeitpunkt, Dateiname, Dateityp und Quelle
- eindeutige Verknüpfung mit Buchung oder Geschäftsvorfall
- keine stille Ersetzung vorhandener Dateien
- neue Fassungen nur als zusätzliche Version
- OCR- oder KI-Ergebnisse bleiben vom Original getrennt

### 5.2 Aufbewahrung

Das System muss ermöglichen:

- konfigurierbare Aufbewahrungsfristen
- Löschsperren für aufbewahrungspflichtige Unterlagen
- dokumentierte Ausnahmen und rechtmäßige Löschprozesse
- vollständigen Export aller Originale und Metadaten
- Nachweis, dass ein exportierter Beleg unverändert ist

### 5.3 KI- und OCR-Verarbeitung

Automatisch ermittelte Werte dürfen nur kontrolliert übernommen werden:

- Originalbeleg bleibt führend
- erkannte Werte werden als Vorschlag gekennzeichnet
- Übernahme und Korrektur werden protokolliert
- Modell, Anbieter und Verarbeitungszeitpunkt sind nachvollziehbar
- Fehlverarbeitung darf den Originalbeleg nicht verändern
- Datenschutz- und Auftragsverarbeitungsanforderungen sind dokumentiert

## 6. Prüfexport und Datenzugriff

Neben DATEV ist ein eigener, vollständiger Prüfexport erforderlich.

Der Export muss mindestens enthalten:

- Mandanten und Gesellschaften
- Geschäftsjahre und Perioden
- Konten und Kontenänderungen
- Steuercodes
- Buchungen und Buchungszeilen
- Festschreibungen und Stornos
- Belegindex
- Originalbelege
- Audit-Log
- offene Posten und Ausgleiche
- Bankimporte, Zuordnungen und Buchungen
- Anlagen und Abschreibungen
- UStVA-Snapshots
- Abschluss- und Ergebnisvortragsbuchungen
- Benutzer, Rollen und Berechtigungen in prüfungsrelevanter Form

Technische Anforderungen:

- maschinenlesbare Formate, bevorzugt CSV und JSON
- vollständige Feldbeschreibung
- konsistente Primär- und Fremdschlüssel
- Manifest mit Dateiliste, Softwareversion und Exportparametern
- Prüfsumme je Datei
- Gesamtprüfsumme des Exportpakets
- reproduzierbare Exporte bei gleichem Datenstand und gleichen Parametern
- keine stillen Filter

## 7. Rollen und Berechtigungen

Erforderlich ist ein dokumentiertes Berechtigungskonzept.

Mindestens zu unterscheiden:

- Systemadministrator
- fachlicher Administrator
- Buchhalter
- Freigeber oder Abschlussverantwortlicher
- Prüfer mit Leserechten
- technischer Servicezugang

Anforderungen:

- Minimalprinzip
- Mandantentrennung
- Trennung unvereinbarer Rollen
- keine gemeinsamen Benutzerkonten
- sichere Passwortspeicherung
- optional Mehrfaktor-Authentisierung für privilegierte Konten
- regelmäßige Rezertifizierung von Berechtigungen
- Protokollierung von Anlage, Änderung und Entzug von Rechten
- zeitliche Begrenzung von Notfallzugängen

## 8. Perioden und Jahresabschluss

Das System muss sicherstellen:

- Perioden können kontrolliert gesperrt werden
- gesperrte Perioden verhindern neue oder geänderte Buchungen
- Entsperrungen sind besonders berechtigt und vollständig protokolliert
- abgeschlossene Geschäftsjahre sind geschützt
- Ergebnisvorträge sind nachvollziehbar
- Abschlussbuchungen sind eindeutig gekennzeichnet
- Eröffnungswerte stimmen mit den Vorjahresendwerten überein
- Stornos in Folgeperioden bleiben nachvollziehbar

## 9. Fachliche Testanforderungen

Für jede wesentliche Funktion sind dokumentierte Soll-Ist-Tests erforderlich.

### 9.1 Kernbuchhaltung

- einfache Buchung
- Splitbuchung
- Soll-Haben-Ungleichheit
- fehlendes Konto
- gesperrte Periode
- Festschreibung
- Storno
- Doppelstorno
- Buchung in fremdem Mandanten
- Rundungsfälle

### 9.2 Umsatzsteuer

- Umsatzsteuer 19 Prozent
- Umsatzsteuer 7 Prozent
- Vorsteuer 19 Prozent
- Vorsteuer 7 Prozent
- steuerfreie Umsätze
- Netto- und Bruttofälle
- Storno steuerhaltiger Buchungen
- Rundungsdifferenzen
- Zeitraumsauswertung
- UStVA-Snapshot

### 9.3 OPOS und Bank

- Anlage offener Posten
- Teilzahlung
- Vollausgleich
- Überzahlung
- Zuordnung zu Bankumsatz
- Deduplizierung beim Import
- falsche Zuordnung und Korrektur

### 9.4 Anlagenbuchhaltung

- Zugang
- lineare AfA
- degressive AfA
- Wechsel zur linearen AfA
- Leistungs-AfA
- GWG
- Sammelposten
- außerplanmäßige Abschreibung
- Abgang
- Restwert und Erinnerungswert

### 9.5 Belege und Exporte

- Originalerhalt
- Hashprüfung
- Versionsbildung
- Downloadberechtigung
- vollständiger Prüfexport
- DATEV-Export
- Export und Reimport zur Plausibilisierung

Für jeden Testfall sind zu dokumentieren:

- Test-ID
- Anforderung
- Vorbedingungen
- Eingabedaten
- erwartetes Ergebnis
- tatsächliches Ergebnis
- Tester
- Datum
- verwendete Softwareversion
- Nachweis, zum Beispiel Log oder Screenshot

## 10. Entwicklungs- und Änderungsprozess

Für eine Prüfung nach IDW PS 880 ist nicht nur das Ergebnis, sondern auch der Entwicklungsprozess relevant.

Erforderlich sind:

- dokumentierte Anforderungen
- Zuordnung von Anforderungen zu Implementierung und Tests
- verpflichtende Code-Reviews
- geschützter Hauptbranch
- automatisierte Tests
- Linting und Sicherheitsprüfungen
- Migrationsprüfung
- reproduzierbare Builds
- versionierte Releases
- Freigabeprozess
- geregelte Fehlerbehebung
- dokumentierter Notfall-Patch-Prozess
- Trennung von Entwicklung, Test und Produktion
- Nachweis der eingesetzten Fremdkomponenten
- Software-Bill-of-Materials

## 11. Release- und Versionsmanagement

Jedes produktive Release benötigt:

- eindeutige Versionsnummer
- Release-Tag
- Commit-SHA
- Änderungsprotokoll
- Datenbankschemaversion
- Liste relevanter Abhängigkeiten
- Prüfsumme der Artefakte
- Freigabenachweis
- Testbericht
- bekannte Einschränkungen
- Migrations- und Rückfallanweisung

Releases für einen Prüfungsgegenstand sollten signiert und unveränderlich archiviert werden.

## 12. Betrieb, Datensicherung und Wiederanlauf

### 12.1 Produktionsbetrieb

- PostgreSQL als bevorzugte produktive Datenbank
- SQLite nur für Entwicklung, Demo oder klar begrenzte Einzelplatzszenarien
- TLS für Webzugriffe
- sichere Secret-Verwaltung
- Härtung von Container und Host
- zentrale Protokollierung
- Zeit-Synchronisation
- Überwachung kritischer Dienste

### 12.2 Datensicherung

- automatisierte tägliche Backups
- verschlüsselte Speicherung
- definierte Aufbewahrungszyklen
- getrennte Speicherung vom Produktivsystem
- Protokollierung von Erfolg und Fehlern
- regelmäßige Restore-Tests
- dokumentierte Wiederanlaufzeiten
- Nachweis, dass Belege und Datenbank konsistent wiederhergestellt werden

## 13. Sicherheitsanforderungen

Mindestens erforderlich:

- sichere Passwort-Hashes
- CSRF-Schutz
- Session-Schutz
- Rate Limiting
- Upload-Allowlist
- Malware-Prüfung von Uploads
- Begrenzung von Dateigrößen
- Schutz vor Mandantendurchgriff
- Schutz vor SQL-Injection, XSS und unsicheren Objektzugriffen
- automatisierte Abhängigkeitsprüfungen
- regelmäßige Penetrationstests oder PenTest-Light
- dokumentiertes Schwachstellenmanagement
- geregelte Sicherheitsupdates

## 14. Erforderliche Dokumentation

Unter `docs/compliance/` sollten mindestens folgende Dokumente entstehen:

- `anforderungen-idw-ps-880-gobd.md`
- `verfahrensdokumentation.md`
- `softwarebeschreibung.md`
- `berechtigungskonzept.md`
- `entwicklungs-und-freigabeprozess.md`
- `testkonzept.md`
- `testfallkatalog.md`
- `backup-und-wiederanlauf.md`
- `installations-und-betriebshandbuch.md`
- `pruefexport-spezifikation.md`
- `release-checkliste.md`
- `gobd-kriterienmatrix.md`

## 15. GoBD-Kriterienmatrix

Für jede Anforderung sollte eine Matrix geführt werden mit:

| ID | Anforderung | Typ | Umsetzung | Testnachweis | Dokumentation | Status |
|---|---|---|---|---|---|---|
| G-001 | Festgeschriebene Buchungen sind unveränderbar | technisch | offen | offen | dieses Dokument | offen |
| G-002 | Korrekturen erfolgen durch Storno | fachlich | teilweise umgesetzt | zu ergänzen | dieses Dokument | in Arbeit |
| G-003 | Belegoriginal bleibt unverändert | technisch | zu prüfen | offen | dieses Dokument | offen |
| G-004 | Audit-Log ist vollständig und manipulationsgeschützt | technisch | teilweise umgesetzt | zu ergänzen | dieses Dokument | in Arbeit |
| G-005 | Vollständiger Prüfexport ist verfügbar | fachlich/technisch | offen | offen | dieses Dokument | offen |
| G-006 | Restore wird regelmäßig getestet | organisatorisch | offen | Restore-Protokoll | Backup-Konzept | offen |

Die Matrix soll im Projektverlauf vollständig erweitert und regelmäßig aktualisiert werden.

## 16. Empfohlene Reihenfolge

### Phase A: Readiness

1. Prüfungsgegenstand festlegen
2. GoBD-Kriterienmatrix vervollständigen
3. Verfahrensdokumentation erstellen
4. Datenbankmanipulationsschutz ergänzen
5. Belegarchiv härten
6. Prüfexport implementieren

### Phase B: Nachweise

1. Testfallkatalog erstellen
2. fachliche Referenzfälle definieren
3. automatisierte und manuelle Tests ausführen
4. Release- und Freigabeprozess dokumentieren
5. Restore-Test durchführen
6. Sicherheitsprüfung durchführen

### Phase C: Externe Prüfung

1. Readiness Assessment durch IT-Prüfer
2. Lücken schließen
3. Vorprüfung
4. Prüfungsgegenstand einfrieren
5. Softwareprüfung nach IDW PS 880

## 17. Definition of Done für Prüfungsreife

OpenBuchhaltung gilt intern als bereit für eine externe Vorprüfung, wenn:

- der Prüfungsgegenstand eindeutig definiert ist
- alle Muss-Anforderungen einer verantwortlichen Person zugeordnet sind
- die GoBD-Kriterienmatrix keine ungeklärten kritischen Punkte enthält
- Festschreibung auch auf Datenbankebene geschützt ist
- das Audit-Log manipulationsgeschützt und exportierbar ist
- Originalbelege unverändert und hashgesichert gespeichert werden
- ein vollständiger Prüfexport vorhanden ist
- alle Kernprozesse dokumentierte Testnachweise besitzen
- Release und Datenbankschema eindeutig reproduzierbar sind
- Rollen, Backup, Restore und Betrieb dokumentiert sind
- eine vollständige Verfahrensdokumentation vorliegt
- ein unabhängiges Readiness Assessment keine wesentlichen ungeklärten Mängel mehr feststellt

## 18. Verantwortung

Die Software kann ordnungsmäßige Prozesse unterstützen, aber nicht allein garantieren. Der jeweilige Betreiber bleibt verantwortlich für:

- sachgerechte Konfiguration
- korrekte Stammdaten
- zeitgerechte Buchung und Festschreibung
- Vergabe und Kontrolle von Berechtigungen
- Verfahrensdokumentation des konkreten Betriebs
- Datensicherung und Aufbewahrung
- Einhaltung steuerlicher und handelsrechtlicher Pflichten

Dieses Dokument ist eine technische und organisatorische Arbeitsgrundlage und keine Rechts- oder Prüfungsberatung.