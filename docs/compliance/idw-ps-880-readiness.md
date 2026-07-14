# IDW-PS-880-Readiness

Dieses Dokument dient als Vorbereitung auf eine moegliche Softwarepruefung. Es ist keine Bescheinigung und ersetzt keine Pruefung durch einen Wirtschaftspruefer.

## 1. Pruefgegenstand definieren

Vor einer externen Pruefung muss eindeutig festgelegt werden, was geprueft werden soll.

| Bereich | Festlegung |
|---|---|
| Produkt | OpenBuchhaltung |
| Version | noch festzulegen, z. B. `1.0-compliance-candidate` |
| Repository | `unikinCK/OpenBuchhaltung` |
| Commit | Release-Commit des Pruefgegenstands |
| Datenbankschema | Alembic-Head des Release-Commits |
| Betriebsmodell | Docker/Serverbetrieb mit PostgreSQL oder MariaDB |
| Ausgeschlossene Bereiche | z. B. unsichere Entwicklungsmodi, lokale Demo-Daten, SQLite-Entwicklung |
| Schnittstellen | UI, REST-API, MCP, DATEV-/Prueferexport, Importfunktionen |
| Zielnutzer | Kapitalgesellschaften, Steuerberater, Prueferrollen |

## 2. Erwartete Pruefungsunterlagen

| ID | Unterlage | Inhalt | Status |
|---|---|---|---|
| DOCU-001 | Softwarebeschreibung | Zweck, Module, Funktionsumfang, Grenzen, Datenmodell, Schnittstellen | offen |
| DOCU-002 | Architekturkonzept | Schichten, Komponenten, Datenfluesse, Sicherheitsgrenzen | offen |
| DOCU-003 | Verfahrensdokumentation | Einsatz, Prozesse, Rollen, Belege, Buchungen, Archivierung, Export | offen |
| DOCU-004 | Berechtigungskonzept | Rollen, Rechte, Mandantentrennung, API/MCP-Zugriffe | offen |
| DOCU-005 | Testkonzept | Testarten, Testdaten, Testabdeckung, Negativtests | offen |
| DOCU-006 | Testnachweise | CI-Protokolle, manuelle Testberichte, fachliche Testmatrix | offen |
| DOCU-007 | Release-Dokumentation | Version, Commit, Migrationen, Changelog, Pruefsummen, Freigabe | offen |
| DOCU-008 | Betriebsdokumentation | Installation, Konfiguration, Backup, Restore, Monitoring, Updates | offen |
| DOCU-009 | Sicherheitskonzept | Authentisierung, Autorisierung, Secrets, Uploads, Protokolle, Adminzugriffe | offen |
| DOCU-010 | Datenexportbeschreibung | Tabellen, Felder, Formate, Manifest, Hashes, Beispielpaket | teilweise |

## 3. Produktkontrollen

| ID | Kontrolle | Erwartung | Nachweis | Status |
|---|---|---|---|---|
| CTRL-001 | Buchungsvalidierung | Keine unausgeglichenen Buchungen; klare Fehler bei ungueltigen Eingaben | Unit/E2E | teilweise |
| CTRL-002 | Steuerlogik | USt/VSt wird nachvollziehbar aus Steuercodes und Konten abgeleitet | Fachtests | teilweise |
| CTRL-003 | Periodensperre | Schreibzugriffe in gesperrte Perioden werden verhindert | E2E/API | teilweise |
| CTRL-004 | Festschreibung | Direktaenderungen festgeschriebener Buchungen sind technisch ausgeschlossen | DB/API/UI | umgesetzt |
| CTRL-005 | Storno | Korrekturen erfolgen ueber nachvollziehbare Gegenbuchungen | Fachtests | teilweise |
| CTRL-006 | Audit-Log | Kritische Aktionen sind vollstaendig, unveraenderbar und auswertbar protokolliert | Audit-Test | teilweise |
| CTRL-007 | Belegarchiv | Originalbelege bleiben erhalten und sind gegen stille Ersetzung geschuetzt | Hash/Versionstest | offen |
| CTRL-008 | Export | Vollstaendiger Prueferexport inklusive Manifest und Belegen | Exporttest | teilweise |
| CTRL-009 | Rollen | Rechte und Mandantentrennung gelten fuer UI, API und MCP einheitlich | Security-Test | teilweise |
| CTRL-010 | Migrationen | Datenbankschema wird nachvollziehbar migriert; Fehler fuehren zu Abbruch | Migrationstest | teilweise |

## 4. Entwicklungsprozess

| ID | Anforderung | Sollzustand | Status |
|---|---|---|---|
| DEV-PS880-001 | Anforderungen versioniert | Anforderungen liegen im Repository und werden mit Releases versioniert. | teilweise |
| DEV-PS880-002 | Issue-Traceability | Jede Compliance-Anforderung hat Issue, Umsetzung, Test und Review. | offen |
| DEV-PS880-003 | Branch Protection | Main-Branch ist geschuetzt; direkte Pushes sind fuer Releases ausgeschlossen. | offen |
| DEV-PS880-004 | Reviewpflicht | PR-Review fuer Compliance- und Kernbuchhaltungsfunktionen. | offen |
| DEV-PS880-005 | CI-Pflicht | Tests, Linting und Migrationen muessen vor Merge erfolgreich sein. | teilweise |
| DEV-PS880-006 | Release-Freigabe | Formaler Freigabeprozess mit Testbericht und Changelog. | offen |
| DEV-PS880-007 | Abhaengigkeiten | Dependencies werden versioniert, geprueft und bei Sicherheitsluecken aktualisiert. | offen |
| DEV-PS880-008 | Fehlerprozess | Kritische Fehler werden klassifiziert, dokumentiert, getestet und nachvollziehbar behoben. | offen |

## 5. Readiness-Phasen

### Phase A: Interne Vorbereitung

- Compliance-Kriterienkatalog finalisieren.
- Bestehende Funktionen gegen Kriterien mappen.
- Offene Luecken als Issues anlegen.
- Testkatalog aufbauen und CI-Abdeckung erhoehen.
- Produktive Referenzkonfiguration definieren.

### Phase B: Compliance-Haertung

- Datenbankseitige Unveraenderbarkeit implementieren. *(fuer festgeschriebene
  Buchungen und Audit-Log umgesetzt; Belege sind API-seitig loeschgeschuetzt und
  werden per Dateipruefsumme kontrolliert)*
- Kryptografische Integritaetsnachweise implementieren. *(Audit-Log mit
  SHA-256-Kette, festgeschriebene Buchungen mit Inhaltshash und Belege mit
  Dateipruefsumme; gemeinsame CLI/API/UI/MCP-Pruefung umgesetzt)*
- Vollstaendigen Prueferexport entwickeln. *(Formatversion 2 mit Feldkatalog,
  Einzelhashes, stabilem Datenbestands-Hash, Rollen und lokaler Paketpruefung
  umgesetzt; fachliche Restfelder und Beispielpaket offen)*
- Verfahrensdokumentation und Betriebshandbuch erstellen.
- Release-Prozess mit signierten oder gehashten Artefakten einfuehren.

### Phase C: Vorpruefung

- IT-pruefungsnahen Wirtschaftspruefer oder IT-Auditor beauftragen.
- Gap-Analyse gegen Produkt und Dokumentation durchfuehren.
- Festgestellte Luecken beheben.
- Testnachweise und Beispielmandant bereitstellen.

### Phase D: Formale Softwarepruefung

- Eindeutige Produktversion einfrieren.
- Pruefungsgegenstand und Abgrenzung dokumentieren.
- Pruefung der Funktionen, Kontrollen und Entwicklungsprozesse begleiten.
- Pruefungsfeststellungen in Folgeversionen nachhalten.

## 6. Akzeptanzkriterien fuer einen Compliance Candidate

Ein Release sollte erst als `compliance-candidate` bezeichnet werden, wenn mindestens folgende Punkte erfuellt sind:

- Alle Kernanforderungen aus `gobd-kriterienkatalog.md` sind `umgesetzt` oder bewusst als organisatorische Pflicht abgegrenzt.
- Es gibt eine vollstaendige Verfahrensdokumentation fuer einen Referenzbetrieb.
- Es gibt einen reproduzierbaren Demo-/Pruefmandanten mit Testdaten.
- Der Prueferexport enthaelt alle steuerlich relevanten Daten und Belege.
- Festgeschriebene Buchungen, Belege und Audit-Eintraege sind gegen stille Veraenderung geschuetzt.
- CI und fachliche Testmatrix laufen fuer den Release-Commit erfolgreich durch.
- Release-Artefakte, Container-Images und Dokumentation sind eindeutig versioniert.
