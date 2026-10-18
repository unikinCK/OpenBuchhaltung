# systemd-Timer für den Produktivbetrieb

Zwei Timer automatisieren die in `docs/compliance/produktionsbetrieb.md`
geforderten Routinen: tägliches Backup (`backup.sh`) und tägliche
Integritätsprüfung (`flask verify-integrity` im laufenden `app`-Container).

Installation (Pfade anpassen, Default `/opt/openbuchhaltung`):

```bash
sudo cp deploy/systemd/openbuchhaltung-*.service deploy/systemd/openbuchhaltung-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openbuchhaltung-backup.timer openbuchhaltung-verify-integrity.timer
systemctl list-timers 'openbuchhaltung-*'
```

Manuell auslösen und Protokoll lesen:

```bash
sudo systemctl start openbuchhaltung-verify-integrity.service
journalctl -u openbuchhaltung-verify-integrity.service -n 50
journalctl -u openbuchhaltung-backup.service -n 50
```

`verify-integrity` beendet sich bei Abweichungen mit Fehlerstatus; die
Service-Unit gilt dann als `failed` (`systemctl --failed`). Für Alarmierung eine
`OnFailure=`-Unit (Mail, Webhook) ergänzen. Das Backup-Ziel `BACKUP_DIR` in
`openbuchhaltung-backup.service` sollte auf ein getrenntes, verschlüsseltes
Volume zeigen; `BACKUP_KEEP` steuert die Anzahl aufbewahrter Sicherungen.
