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

Softwareversion, Commit und Migrationsstand liefert die Anwendung selbst:
`GET /api/v1/health` gibt `version` (aus `pyproject.toml`, Release-Tag
`v<Version>`), `commit`, die Alembic-Revision der Datenbank und den erwarteten
Head aus; das Pruefermanifest enthaelt den Commit als `commit_sha`. `redeploy.sh`
protokolliert Commit und Tag jedes Ausrollens, `backup.sh` legt sie in
`meta.txt` jeder Sicherung ab. Die Aenderungen je Version stehen in
`CHANGELOG.md`.

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

### 6.1 Backup mit `backup.sh`

`backup.sh` (Repo-Root) sichert den laufenden Produktions-Stack
(`docker-compose.production.yml`) in ein Verzeichnis
`BACKUP_DIR/openbuchhaltung-<JJJJMMTT-HHMMSS>/` (Default `./backups`):

| Datei | Inhalt |
|---|---|
| `db.dump` | `pg_dump --format=custom` der Datenbank (Buchungen, Belegmetadaten, Audit-Log, Migrationsstand) |
| `instance.tar.gz` | `/app/instance` aus dem Volume `app_data` (Belegdateien und -versionen) |
| `meta.txt` | Zeitpunkt, Host, Git-Commit/-Tag, Alembic-Revision, PostgreSQL-Version, Image |
| `SHA256SUMS` | Pruefsummen der drei Dateien |

`BACKUP_KEEP` (Default 14) begrenzt die Anzahl aufbewahrter Sicherungen.
`redeploy.sh` ruft `backup.sh` vor jedem `compose down` auf. Die Konfiguration
(`.env`) wird nicht mitgesichert, weil sie Secrets enthaelt; sie ist getrennt
(Secret Store) zu sichern. Das Backup-Verzeichnis gehoert verschluesselt (z. B.
`age`/`gpg` oder verschluesseltes Volume) und getrennt vom Produktivsystem
abgelegt; die Uebertragung erfolgt z. B. per `rsync`/`restic`.

Taegliche Ausfuehrung: systemd-Timer `deploy/systemd/openbuchhaltung-backup.timer`
(Installation siehe `deploy/systemd/README.md`), alternativ ein Cron-Eintrag
`30 2 * * * cd /opt/openbuchhaltung && BACKUP_DIR=/var/backups/openbuchhaltung ./backup.sh`.
Das Backup-Log (`journalctl -u openbuchhaltung-backup.service`) dient als Nachweis.

### 6.2 Restore-Runbook

Voraussetzung: ein Backup-Verzeichnis `B` mit gueltigen Pruefsummen
(`cd B && sha256sum -c SHA256SUMS`) und der in `meta.txt` genannte Codestand.
Alle Befehle im Repo-Root; `C` steht fuer
`docker compose -f docker-compose.production.yml`.

1. **Anlass dokumentieren** (Restore-Protokoll: Zeitpunkt, Grund, verwendetes
   Backup, ausfuehrende Person).
2. **Codestand herstellen:** `git checkout <git_commit oder Tag aus meta.txt>`;
   danach `C build`. Wird ein aelteres Backup in einen neueren Codestand
   eingespielt, uebernimmt Schritt 6 die Nachmigration.
3. **Anwendung stoppen, Datenbank starten:** `C stop app` und `C up -d db`.
4. **Datenbank zuruecksetzen und einspielen** (`openbuchhaltung` ist der
   Superuser des mitgelieferten Images):
   ```bash
   C exec -T db psql -U openbuchhaltung -d postgres \
     -c 'DROP DATABASE IF EXISTS openbuchhaltung;' \
     -c 'CREATE DATABASE openbuchhaltung OWNER openbuchhaltung;'
   C exec -T db pg_restore -U openbuchhaltung -d openbuchhaltung \
     --no-owner --exit-on-error < B/db.dump
   C exec -T db psql -U openbuchhaltung -d openbuchhaltung -Atc \
     'SELECT version_num FROM alembic_version'   # muss alembic_revision aus meta.txt sein
   ```
5. **Belegablage einspielen** (ersetzt den Inhalt des Volumes `app_data`):
   ```bash
   C run --rm --no-deps -T --user root app sh -c \
     'rm -rf /app/instance/* && tar -xzf - -C /app && chown -R app:app /app/instance' \
     < B/instance.tar.gz
   ```
6. **Schema auf den Codestand bringen:** `C run --rm app alembic upgrade head`
   (ohne Aenderung, wenn Backup und Code zusammenpassen).
7. **Starten und pruefen:** `C up -d`, dann
   `curl -s http://127.0.0.1:8000/api/v1/health` (erwartet `"status": "ok"`,
   `schema.up_to_date: true`) und die Integritaetspruefung
   `C exec app flask --app run.py verify-integrity` (Buchungshashes, Belegdateien,
   Audit-Hashkette). Anschliessend fachlicher Smoke-Test (Anmeldung, Journal,
   Belegansicht, Bericht).
8. **Protokoll abschliessen** (Ergebnis, Abweichungen, Dauer). Bei
   Restore-**Tests** anschliessend die Testinstanz wieder entfernen
   (`C down -v` nur auf der Testinstanz!).

Fuer einen Restore-Test ohne Eingriff in die Produktion denselben Ablauf mit einem
eigenen Compose-Projekt ausfuehren (`COMPOSE_PROJECT_NAME=obk-restoretest`,
`APP_PORT=18000`); Volumes und Container bleiben dadurch getrennt.

### 6.3 Automatisierte Integritaetspruefung

`deploy/systemd/openbuchhaltung-verify-integrity.timer` fuehrt taeglich
`flask --app run.py verify-integrity` im laufenden `app`-Container aus. Der Lauf
migriert nichts (`DB_AUTO_MIGRATE=0`) und endet bei Abweichungen mit Fehlerstatus,
sodass die Unit als `failed` sichtbar wird (`systemctl --failed`,
`journalctl -u openbuchhaltung-verify-integrity.service`). Das Journal ist das
Pruefprotokoll im Sinne von Abschnitt 3.

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

Referenzablauf mit `redeploy.sh` (nur `docker-compose.production.yml`):

1. `git pull --ff-only`; Commit und Release-Tag werden ausgegeben und gehoeren ins
   Updateprotokoll.
2. Backup der laufenden Instanz ueber `backup.sh` (vor dem Stoppen).
3. `compose down`, `compose build` (Commit als Build-Arg), Eigentuemer der
   Belegablage im Volume korrigieren.
4. `alembic upgrade head` als eigener, einmaliger Schritt. Der `app`-Service laeuft
   mit `DB_AUTO_MIGRATE=0`, migriert also nie selbst und startet nicht, wenn das
   Schema nicht auf dem erwarteten Head steht (fail-fast statt Schemafehler im
   Betrieb; kein Rennen zwischen mehreren Workern).
5. `compose up -d` und Warten auf den Health-Check (`/api/v1/health`).
6. Nachtest: `verify-integrity` (siehe 6.3) und fachlicher Smoke-Test.

Schlaegt die Migration fehl, bleibt die Anwendung gestoppt; Rueckweg ist das
Restore aus Schritt 2 (Abschnitt 6.2) mit dem vorherigen Codestand.

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

Die gemeinsamen Integritaetsnachweise sollten nach Updates und regelmaessig im
Betrieb geprueft werden:

```bash
flask --app run.py verify-integrity
```

Der Befehl kontrolliert festgeschriebene Buchungen, Belegdateien und
Audit-Hashketten. Er beendet sich bei erkannten Abweichungen mit einem
Fehlerstatus und eignet sich damit fuer Monitoring; die taegliche Ausfuehrung
uebernimmt der systemd-Timer aus Abschnitt 6.3. Die engere Pruefung
`verify-audit-log` bleibt fuer gezielte Audit-Diagnosen verfuegbar.

Anwendungslogs tragen je Eintrag die Request-ID (`X-Request-ID`, vom
Reverse-Proxy uebernommen oder erzeugt und in der Antwort zurueckgegeben); mit
`LOG_FORMAT=json` lassen sie sich strukturiert in eine zentrale Protokollablage
uebernehmen. Der gunicorn-Access-Log enthaelt Dauer und Request-ID jedes Aufrufs.

## 10. Export und Weitergabe an Dritte

Bei Exporten an Steuerberater, Wirtschaftspruefer oder Betriebspruefer:

- Exportzeitpunkt dokumentieren
- Zeitraum und Parameter dokumentieren
- Empfaenger dokumentieren
- Exportmanifest aufbewahren
- Hash des Exportpakets speichern
- sichere Uebertragung verwenden
- Zugriffe und Downloads protokollieren

## 11. Bankanbindung (FinTS)

Fuer den FinTS/HBCI-Direktabruf gelten folgende Betriebsregeln:

- `FINTS_PRODUCT_ID` muss auf eine bei der Deutschen Kreditwirtschaft
  registrierte Produktkennung gesetzt sein (<https://www.fints.org>);
  ohne Kennung verweigert die Anwendung den Abruf.
- PIN und TAN werden zu keinem Zeitpunkt persistiert oder protokolliert;
  die PIN wird bei jedem Abruf (und bei der TAN-Bestaetigung erneut)
  eingegeben.
- Wartet ein Abruf auf eine TAN, wird der Dialogzustand serialisiert in
  `fints_pending_dialog` zwischengespeichert (ohne PIN). Diese Datensaetze
  verfallen nach 15 Minuten und werden bei Abschluss, Fehler oder Abbruch
  geloescht. Sie liegen unverschluesselt in der Datenbank — wer die
  DB-Datei als entsprechend schutzwuerdig einstuft, verschluesselt sie
  auf Volume- oder Datenbankebene.
- Bankzugaenge (BLZ, Login, FinTS-URL) sind Stammdaten je Gesellschaft;
  das Anlegen und Deaktivieren wird im Audit-Log protokolliert.

## 12. Mindestabnahme vor Produktivstart

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
