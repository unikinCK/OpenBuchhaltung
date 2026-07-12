# Gliederung Verfahrensdokumentation

Diese Gliederung kann als Grundlage fuer eine Verfahrensdokumentation zu OpenBuchhaltung verwendet werden. Sie muss fuer jeden produktiven Anwender konkretisiert werden, weil Betrieb, Rollen, Backup, Verantwortlichkeiten und organisatorische Kontrollen unternehmensspezifisch sind.

## 1. Allgemeine Angaben

- Unternehmen / Mandant
- Verantwortliche Personen
- eingesetzte OpenBuchhaltung-Version
- Installationsort / Hostingmodell
- Datenbank und Dateispeicher
- Beginn der produktiven Nutzung
- betroffene Geschaeftsprozesse
- abgegrenzte Vorsysteme und nachgelagerte Systeme

## 2. Systemuebersicht

- Zweck der Software
- fachlicher Funktionsumfang
- technische Architektur
- UI, REST-API, MCP und Import-/Export-Schnittstellen
- Datenbankmodell auf hoher Ebene
- Belegablage
- Authentisierung und Autorisierung
- Protokollierung und Monitoring

## 3. Rollen und Verantwortlichkeiten

| Rolle | Aufgaben | Rechte in OpenBuchhaltung | Verantwortlich |
|---|---|---|---|
| Geschaeftsfuehrung | Ordnungsgemaesse Buchfuehrung, Freigaben, Organisation | optional Admin/Leser | zu ergaenzen |
| Administrator | Benutzer, Mandanten, technische Konfiguration, Backups | Admin | zu ergaenzen |
| Buchhaltung | Belege, Buchungen, OPOS, Bank, Anlagen, UStVA | Buchhalter | zu ergaenzen |
| Pruefer/Steuerberater | Einsicht, Auswertungen, Export, Rueckfragen | Pruefer/Leser | zu ergaenzen |
| IT-Betrieb | Server, Datenbank, Backup, Restore, Updates | technischer Zugriff | zu ergaenzen |

## 4. Belegprozess

### 4.1 Eingang

- zulaessige Belegquellen
- zulaessige Dateiformate
- Erfassungszeitpunkt
- Verantwortlichkeit fuer Vollstaendigkeit
- Umgang mit Papierbelegen
- Umgang mit E-Rechnungen

### 4.2 Speicherung

- Ablageort der Originaldateien
- Hashbildung beim Eingang
- Versionierung bei Korrekturen
- Schutz gegen Loeschung oder Ueberschreibung
- Zuordnung zu Buchungen, OPOS, Anlagen oder Bankumsaetzen

### 4.3 Kontrolle

- Vollstaendigkeitspruefung
- Beleg-Buchungs-Abgleich
- Umgang mit fehlenden oder fehlerhaften Belegen
- Protokollierung von Aenderungen

## 5. Buchungsprozess

- Erfassung manueller Buchungen
- Import aus Bank-CSV
- Import und Verarbeitung von E-Rechnungen
- automatische Steuerzeilen
- OPOS-Erfassung und Ausgleich
- Anlagenzugang, Abschreibung, Abgang
- Plausibilitaetskontrollen
- Fehlerbehandlung

## 6. Festschreibung und Korrekturen

- Zeitpunkt und Verantwortlichkeit der Festschreibung
- Einzelfestschreibung
- Festschreibelauf bis Datum
- technische Wirkung der Festschreibung
- Storno-/Gegenbuchungsprozess
- Verbot direkter Aenderung festgeschriebener Buchungen
- Dokumentation von Korrekturgruenden

## 7. Perioden und Jahresabschluss

- Einrichtung von Geschaeftsjahren und Perioden
- Periodensperren
- Entsperrprozess und Genehmigung
- Jahresabschlussprozess
- Ergebnisvortrag
- Umgang mit Nachbuchungen
- Abstimmung mit Steuerberater/Wirtschaftspruefer

## 8. Auswertungen und Meldungen

- Summen- und Saldenliste
- GuV
- Bilanz
- BWA, soweit umgesetzt
- UStVA-Berechnung und Festhalten von Snapshots
- DATEV-Export
- Prueferexport
- Dokumentation von Exportzeitpunkt, Parametern und Empfaenger

## 9. Datenzugriff fuer Pruefungen

- Prueferrolle und Rechte
- Exportpakete
- Feld- und Tabellenbeschreibung
- Belegindex
- Bereitstellung fuer Steuerberater, Wirtschaftspruefer oder Finanzverwaltung
- Protokollierung von Exporten

## 10. Benutzer- und Rechteverwaltung

- Anlage von Benutzern
- Vergabe von Rollen
- Mandantenzuordnung
- API-/MCP-Token
- Passwortregeln
- Deaktivierung ausgeschiedener Benutzer
- regelmaessige Rechtepruefung

## 11. Technischer Betrieb

- Serverumgebung
- Datenbank
- Dateispeicher
- Secrets und Umgebungsvariablen
- TLS/HTTPS
- Systemzeit und Zeitzone
- Logs und Monitoring
- Updates und Migrationen

## 12. Datensicherung und Wiederherstellung

- Backupumfang: Datenbank, Belege, Konfiguration, Logs
- Backuphaufigkeit
- Aufbewahrungsdauer
- Verschluesselung
- Zugriffsschutz
- Restore-Test
- Verantwortlichkeit
- Dokumentationspflicht je Restore-Test

## 13. Aenderungsmanagement

- Releaseprozess
- Test- und Freigabeprozess
- Migrationen
- Notfallpatches
- Rollbackstrategie
- Dokumentation von Aenderungen
- Information an Benutzer

## 14. Kontrollhandlungen

| Kontrolle | Frequenz | Verantwortlich | Nachweis |
|---|---|---|---|
| Rechtepruefung | monatlich/quartalsweise | zu ergaenzen | Protokoll |
| Backupkontrolle | taeglich/woechentlich | zu ergaenzen | Backup-Log |
| Restore-Test | mindestens jaehrlich | zu ergaenzen | Restore-Protokoll |
| Festschreibung | monatlich/quartalsweise | zu ergaenzen | Audit-Log |
| Belegvollstaendigkeit | laufend/periodisch | zu ergaenzen | Checkliste |
| Export fuer Steuerberater | nach Bedarf | zu ergaenzen | Exportmanifest |
| Integritaetspruefung | regelmaessig | zu ergaenzen | Pruefbericht |

## 15. Anlagen

- Systemarchitektur
- Rollenmatrix
- Datenmodellbeschreibung
- Schnittstellenbeschreibung
- Testnachweise
- Release-Dokumentation
- Backup-/Restore-Protokolle
- Exportbeispiele
- Kriterienkatalog
