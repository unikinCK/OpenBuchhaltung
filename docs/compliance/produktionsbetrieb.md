# Anforderungen an den Produktionsbetrieb

Dieses Dokument beschreibt Mindestanforderungen an einen produktiven Betrieb von OpenBuchhaltung. Die Software kann ordnungsmaessige Buchfuehrung nur unterstuetzen; die tatsaechliche Ordnungsgemaessheit haengt auch von Betrieb, Organisation und Nutzung ab.

## 1. Referenzbetrieb

Fuer einen pruefbaren Produktivbetrieb sollte eine Referenzkonfiguration definiert werden.

| Bereich | Empfehlung |
|---|---|
| Anwendung | OpenBuchhaltung Release-Version, kein Entwicklungsserver |
| Datenbank | PostgreSQL oder MariaDB |
| Belegablage | persistenter, gesicherter Dateispeicher oder Objektstorage |
| Deployment | Container oder reproduzierbare Serverinstallation |
| Transport | HTTPS/TLS |
| Authentisierung | individuelle Benutzerkonten, sichere Passwoerter, optional SSO/MFA |
| Secrets | ausserhalb des Repositories, z. B. Secret Store oder sichere ENV-Konfiguration |
| Logs | zentrale und geschuetzte Protokollablage |
| Backup | automatisiert, verschluesselt, regelmaessig getestet |

## 2. Unzulaessige oder nur fuer Entwicklung geeignete Betriebsarten

Folgende Betriebsarten sollten nicht als ordnungsmaessiger Produktivbetrieb gelten:

- Flask-Entwicklungsserver ohne produktionsgeeigneten WSGI/ASGI-Server
- SQLite fuer Mehrbenutzer- oder produktive Mandanten
- Demo-Benutzer oder Standardpasswoerter
- deaktivierte Authentisierung fuer API/MCP
- Speicherung von Belegen in nicht gesicherten temporaeren Verzeichnissen
- fehlende Backups
- fehlende Restore-Tests
- direkte Datenbankaenderungen ohne dokumentierten Notfallprozess

## 3. Verantwortlichkeiten

| Aufgabe | Verantwortlich | Nachweis |
|---|---|---|
| Benutzeranlage und Rollenpflege | Betreiber/Administrator | Audit-Log, Rechteprotokoll |
| Buchungsfreigabe und Festschreibung | Buchhaltung/Geschaeftsfuehrung | Audit-Log |
| Perioden- und Jahresabschluss | Buchhaltung/Geschaeftsfuehrung | Abschlussprotokoll |
| Backup | IT-Betrieb | Backup-Log |
| Restore-Test | IT-Betrieb | Restore-Protokoll |
| Updates | IT-Betrieb/Administrator | Release- und Updateprotokoll |
| Export fuer Pruefer | Buchhaltung/Administrator | Exportmanifest, Audit-Log |
| Integritaetspruefung | Administrator/IT-Betrieb | Pruefprotokoll |

## 4. Installation und Konfiguration

Produktive Installationen muessen dokumentieren:

- Softwareversion und Commit
- Datenbankschema/Migrationsstand
- Betriebsumgebung und Host
- Datenbankverbindung
- Belegablage
- konfigurierte Mandanten und Gesellschaften
- aktivierte Schnittstellen
- Authentisierungsmodus
- API-/MCP-Zugriff
- Backupziel
- Log- und Monitoringziel

## 5. Benutzer und Berechtigungen

Mindestanforderungen:

- Jeder Benutzer hat ein persoenliches Konto.
- Keine geteilten produktiven Benutzerkonten.
- Pruefer erhalten nur Leserechte und Exportrechte.
- Buchhalter erhalten keine administrativen Systemrechte.
- Adminrechte werden auf wenige Personen begrenzt.
- Rechte werden regelmaessig geprueft.
- Ausgeschiedene Benutzer werden unverzueglich deaktiviert.
- API- und MCP-Tokens sind benutzer- oder rollenbezogen und widerrufbar.

## 6. Backup und Restore

Backups muessen mindestens umfassen:

- Datenbank
- Belege und Belegversionen
- Konfiguration
- Migrationsstand
- relevante System- und Audit-Logs
- Exportmanifest historischer Prueferexporte, sofern separat abgelegt

Empfehlungen:

- taegliche automatische Backups
- verschluesselte Ablage
- getrennte Aufbewahrung vom Produktivsystem
- definierte Aufbewahrungsfristen
- regelmaessige Restore-Tests
- Restore-Test mindestens jaehrlich und nach wesentlichen Infrastrukturwechseln

## 7. Updates und Migrationen

Vor jedem Update:

1. Release Notes lesen.
2. Backup erstellen.
3. Migrationen in Testumgebung ausfuehren.
4. fachliche Smoke-Tests ausfuehren.
5. Wartungsfenster dokumentieren.
6. Produktivmigration durchfuehren.
7. Nachtests und Integritaetspruefung durchfuehren.
8. Updateprotokoll ablegen.

Migrationen muessen fail-fast abbrechen, wenn ein nicht erwarteter Datenbankzustand erkannt wird.

## 8. Direkte Datenbankeingriffe

Direkte Eingriffe in die Datenbank sind im Normalbetrieb nicht zulaessig. Falls ein Notfalleingriff unvermeidbar ist:

- vorher Backup erstellen
- Anlass dokumentieren
- Freigabe durch verantwortliche Person einholen
- SQL-Befehle dokumentieren
- Auswirkungen pruefen
- Integritaetspruefung danach ausfuehren
- Vorgang im Betriebsprotokoll ablegen

## 9. Protokollierung und Integritaet

Der Betrieb muss sicherstellen:

- Audit-Log ist fuer Benutzer nicht direkt manipulierbar.
- Systemlogs werden aufbewahrt.
- Zeitquelle ist stabil und dokumentiert.
- Integritaetspruefungen fuer Belege, Audit-Log und Exportpakete werden regelmaessig ausgefuehrt.
- Auffaelligkeiten werden dokumentiert und eskaliert.

Die Audit-Hashkette sollte nach Updates und regelmaessig im Betrieb geprueft werden:

```bash
flask --app run.py verify-audit-log
```

Der Befehl beendet sich bei erkannten Sequenz-, Verknuepfungs- oder
Inhaltsabweichungen mit einem Fehlerstatus und eignet sich damit fuer Monitoring.

## 10. Export und Weitergabe an Dritte

Bei Exporten an Steuerberater, Wirtschaftspruefer oder Betriebspruefer:

- Exportzeitpunkt dokumentieren
- Zeitraum und Parameter dokumentieren
- Empfaenger dokumentieren
- Exportmanifest aufbewahren
- Hash des Exportpakets speichern
- sichere Uebertragung verwenden
- Zugriffe und Downloads protokollieren

## 11. Mindestabnahme vor Produktivstart

Vor Produktivstart sollten folgende Punkte bestaetigt sein:

- produktive Authentisierung aktiv
- starker, installationsspezifischer `SECRET_KEY` gesetzt; Start ohne Secret schlägt fehl
- Demo-Benutzer geloescht oder Passwoerter geaendert
- API und MCP mit getrennten Tokens abgesichert; MCP nicht ungeschützt öffentlich gebunden
- Datenbank produktionsgeeignet
- Belegablage persistent und gesichert
- Backup eingerichtet
- Restore-Test erfolgreich
- Rollen vergeben und dokumentiert
- Verfahrensdokumentation erstellt
- Testmandant erfolgreich durchgebucht
- Exporttest erfolgreich
- Verantwortlichkeiten benannt
