# GoBD-Kriterienkatalog

Dieser Katalog beschreibt Anforderungen, die OpenBuchhaltung fuer eine GoBD-orientierte Nutzung technisch unterstuetzen sollte. Die Bewertung bezieht sich auf das Produkt; organisatorische Pflichten beim Anwender bleiben zusaetzlich bestehen.

## 1. Grundsaetze

| ID | Anforderung | Soll-Umsetzung in OpenBuchhaltung | Nachweis | Status |
|---|---|---|---|---|
| GOBD-001 | Vollstaendigkeit | Alle steuerlich relevanten Geschaeftsvorfaelle muessen lueckenlos erfasst oder importiert werden koennen. | Fachtests Journal, Import, OPOS, Anlagen, Bank, E-Rechnung | teilweise |
| GOBD-002 | Richtigkeit | Buchungen muessen fachlich validiert werden, insbesondere Soll/Haben-Gleichheit, Steuerlogik und Periodenbezug. | Unit-, Integrations- und E2E-Tests | teilweise |
| GOBD-003 | Zeitgerechte Erfassung | Belege und Buchungen muessen mit Erfassungszeitpunkt und Belegdatum getrennt dokumentiert werden. | Datenmodell, Audit-Log, UI/API-Tests | umgesetzt |
| GOBD-004 | Ordnung | Buchungen, Belege, Konten, Perioden und Abschluesse muessen eindeutig sortierbar, auffindbar und exportierbar sein. | Exportpaket, Datenmodellbeschreibung | teilweise |
| GOBD-005 | Nachvollziehbarkeit | Jeder Vorgang muss vom Beleg zur Buchung und von der Buchung zum Beleg nachvollziehbar sein. | Beleg-Buchungs-Link, Audit-Log, Prueferexport | teilweise |
| GOBD-006 | Unveraenderbarkeit | Festgeschriebene Buchungen und steuerlich relevante Dokumente duerfen nicht unbemerkt veraendert oder geloescht werden koennen. | DB-Trigger, Buchungshashes, Belegpruefsummen, Audit-Hashkette, gemeinsame Integritaetspruefung | teilweise |

## 2. Buchungen, Festschreibung und Storno

| ID | Anforderung | Soll-Umsetzung in OpenBuchhaltung | Nachweis | Status |
|---|---|---|---|---|
| BOOK-001 | Vorlaeufige Buchungen | Buchungen duerfen vor Festschreibung korrigierbar sein, muessen aber als vorlaeufig erkennbar bleiben. | UI/API-Test | teilweise |
| BOOK-002 | Festschreibung | Buchungen koennen einzeln oder periodisch festgeschrieben werden. Danach keine direkte Aenderung. | Service- und Integrationstest | umgesetzt |
| BOOK-003 | Technischer Aenderungsschutz | Updates und Deletes festgeschriebener Buchungen muessen auch auf Datenbankebene verhindert werden. | Migration/Trigger-Test | umgesetzt |
| BOOK-004 | Storno statt Loeschung | Korrekturen festgeschriebener Buchungen erfolgen ausschliesslich ueber Gegenbuchungen. | Storno-Testmatrix | teilweise |
| BOOK-005 | Doppelstorno verhindern | Eine Buchung darf nicht mehrfach storniert werden. | Negativtest | teilweise |
| BOOK-006 | Periodensperren | Geschlossene Perioden duerfen fuer Schreibrollen nicht mehr bebuchbar sein. | Perioden-Test | teilweise |
| BOOK-007 | Abschlussjahr sperren | Nach Jahresabschluss darf nicht mehr in das Jahr gebucht werden. | Abschluss-Test | teilweise |
| BOOK-008 | Buchungsnummern | Buchungen muessen eindeutig identifizierbar und chronologisch auswertbar sein. | Datenmodell/Test | offen |
| BOOK-009 | Kryptografische Versiegelung | Festgeschriebene Buchungen erhalten einen reproduzierbaren Inhaltshash ueber Kopfdaten und Zeilen. | Migration, Integritaets- und Manipulationstest | umgesetzt |

## 3. Audit-Log und Manipulationserkennung

| ID | Anforderung | Soll-Umsetzung in OpenBuchhaltung | Nachweis | Status |
|---|---|---|---|---|
| AUD-001 | Vollstaendige Protokollierung | Kritische Aktionen werden protokolliert: Erstellen, Aendern, Festschreiben, Storno, Import, Export, Login, Rollen, Perioden, Abschluss. | Audit-Testmatrix | teilweise |
| AUD-002 | Kontextdaten | Audit-Eintraege enthalten Benutzer, Rolle, Mandant, Gesellschaft, Objekt, Aktion, Zeitpunkt, Quelle und Softwareversion. | Datenmodell/Test | offen |
| AUD-003 | Vorher/Nachher-Werte | Aenderungen an Stammdaten und relevanten Fachdaten speichern alte und neue Werte. | Datenmodell/Test | offen |
| AUD-004 | Append-only | Audit-Eintraege duerfen nicht geaendert oder geloescht werden. | DB-Trigger/Test | umgesetzt |
| AUD-005 | Hashkette | Audit-Eintraege werden kryptografisch verkettet, um nachtraegliche Manipulationen erkennbar zu machen. | Integritaetstest | umgesetzt |
| AUD-006 | Integritaetspruefung | System kann Hashketten und Belegpruefsummen pruefen und Abweichungen melden. | CLI/API/UI/MCP-Pruefung | umgesetzt |

## 4. Belegverwaltung und Archiv

| ID | Anforderung | Soll-Umsetzung in OpenBuchhaltung | Nachweis | Status |
|---|---|---|---|---|
| DOC-001 | Originalerhalt | Hochgeladene Belege werden unveraendert gespeichert. | Hashvergleich | teilweise |
| DOC-002 | Beleghash | Beim Eingang wird ein kryptografischer Hash der Originaldatei gespeichert. | Unit- und API-Test | umgesetzt |
| DOC-003 | Versionierung | Ersetzungen erfolgen nur als neue Version, niemals als stille Ueberschreibung. | Belegversionstest | umgesetzt |
| DOC-004 | Verknuepfung | Belege koennen eindeutig mit Buchungen, OPOS, Anlagen oder Bankumsaetzen verknuepft werden. | Integrationstest | teilweise |
| DOC-005 | Loeschschutz | Steuerlich relevante Belege duerfen nach Zuordnung/Festschreibung nicht geloescht werden. | API-Negativtest | umgesetzt |
| DOC-006 | Belegindex | Ein Exportindex enthaelt Dateiname, Hash, Uploadzeit, Belegdatum, Verknuepfung und Version. | Exporttest | umgesetzt |
| DOC-007 | Aufbewahrung | Aufbewahrungsfristen und Sperren sind technisch abbildbar. | Fachkonzept/Test | offen |

## 5. Export und Datenzugriff

| ID | Anforderung | Soll-Umsetzung in OpenBuchhaltung | Nachweis | Status |
|---|---|---|---|---|
| EXP-001 | DATEV-Export | Buchungsstapel koennen DATEV-kompatibel exportiert werden. | DATEV-Testdateien | teilweise |
| EXP-002 | Prueferexport | Vollstaendiges Exportpaket fuer Wirtschaftspruefer/Betriebspruefer. | Exportpaket-Test | teilweise |
| EXP-003 | Exportumfang | Export enthaelt Buchungen, Zeilen, Konten, Steuercodes, Stammdaten, Audit-Log, Belegindex, Belege, OPOS, Anlagen, Bank, Perioden und Rollen. | Manifest-Test | teilweise |
| EXP-004 | Exportmanifest | Jeder Export enthaelt Manifest mit Version, Zeitraum, Parametern, Tabellen, Dateihashes und Erstellzeit. | Exporttest | umgesetzt |
| EXP-005 | Maschinenlesbarkeit | Exportdaten liegen in stabil dokumentierten JSON-Formaten mit vollstaendigem Feldkatalog vor. | Feldkatalog und Datenbeschreibung | umgesetzt |
| EXP-006 | Reproduzierbarkeit | Gleiche Parameter auf gleichem Datenstand erzeugen einen stabilen Datenbestands-Hash. | Snapshot-Test | umgesetzt |

## 6. Rollen und Berechtigungen

| ID | Anforderung | Soll-Umsetzung in OpenBuchhaltung | Nachweis | Status |
|---|---|---|---|---|
| IAM-001 | Rollenmodell | Admin, Buchhalter und Pruefer/Leser sind getrennt. | Auth-Tests | teilweise |
| IAM-002 | Least Privilege | Pruefer haben nur Lese- und Exportrechte, keine Schreibrechte. | Rollen-Test | teilweise |
| IAM-003 | Mandantentrennung | Benutzer sehen nur erlaubte Mandanten/Gesellschaften. | Cross-Tenant-Test | teilweise |
| IAM-004 | Admin-Aktionen | Rollen-, Mandanten- und Systemeinstellungen werden vollstaendig protokolliert. | Audit-Test | offen |
| IAM-005 | Passwortschutz | Sichere Passwort-Hashes und optionale starke Authentisierung fuer Produktion. | Security-Test/Betriebshandbuch | teilweise |
| IAM-006 | API-Berechtigungen | API- und MCP-Zugriffe unterliegen demselben Rollen- und Tenant-Scope wie die UI. | API/MCP-Test | teilweise |

## 7. Betrieb, Backup und Wiederherstellung

| ID | Anforderung | Soll-Umsetzung in OpenBuchhaltung | Nachweis | Status |
|---|---|---|---|---|
| OPS-001 | Produktionsdatenbank | Produktivbetrieb bevorzugt PostgreSQL oder MariaDB, nicht SQLite. | Betriebshandbuch | organisatorisch |
| OPS-002 | Backup | Regelmaessige automatische Backups fuer Datenbank und Belegablage. | Backup-Protokolle | organisatorisch |
| OPS-003 | Restore-Test | Wiederherstellung wird regelmaessig getestet und dokumentiert. | Restore-Protokoll | organisatorisch |
| OPS-004 | Zugriffsschutz | Betriebssystem, Datenbank und Dateispeicher sind gegen unberechtigte Zugriffe geschuetzt. | Betriebskonzept | organisatorisch |
| OPS-005 | Protokolle | Systemlogs, Audit-Logs und Exportlogs werden ausreichend lange aufbewahrt. | Betriebskonzept | organisatorisch |
| OPS-006 | Zeitquelle | Server nutzt verlaessliche Zeitquelle; Zeitzone und UTC-Bezug sind dokumentiert. | Betriebskonzept/Test | organisatorisch |

## 8. Entwicklungs- und Releaseprozess

| ID | Anforderung | Soll-Umsetzung in OpenBuchhaltung | Nachweis | Status |
|---|---|---|---|---|
| DEV-001 | Anforderungen | Compliance-Anforderungen sind versioniert und testbar dokumentiert. | Dieser Ordner | teilweise |
| DEV-002 | Traceability | Anforderungen sind mit Issues, Pull Requests, Commits und Tests verknuepft. | Traceability-Matrix | offen |
| DEV-003 | Code Review | Aenderungen an Kernfunktionen erfolgen nur ueber Review. | Branch Protection/PR-Regeln | offen |
| DEV-004 | CI | Linting, Tests, Migrationen und Security-Basistests laufen automatisiert. | CI-Protokolle | teilweise |
| DEV-005 | Release-Freigabe | Releases haben Version, Commit, Schema, Changelog, Testbericht und Freigabe. | Release-Artefakt | offen |
| DEV-006 | Signierte Artefakte | Release-Archive und Container-Images werden signiert oder mit Hashes dokumentiert. | Release-Prozess | offen |

## 9. Priorisierte verbleibende Luecken

1. Restluecken im Prueferexport: Kontenaenderungshistorie und Abschlussnachweise.
2. Verfahrensdokumentation und Betriebshandbuch.
3. Traceability von Anforderungen zu Tests und Pull Requests.
4. Externe Readiness-Pruefung vor einer moeglichen IDW-PS-880-Pruefung.
