# Datenbeschreibung Prüferexport

## 1. Zweck und Format

Der OpenBuchhaltung-Prüferexport stellt den Datenstand einer Gesellschaft als
ZIP-Paket mit stabilen, maschinenlesbaren JSON-Dateien bereit. Formatversion 2
enthält neben den Fachdaten einen technischen Feldkatalog, Einzelprüfsummen und
einen reproduzierbaren Gesamtnachweis über den exportierten Datenbestand.

## 2. Paketstruktur

| Pfad | Inhalt |
|---|---|
| `manifest.json` | Version, Erzeugungszeit, Software-Commit, Parameter, Umfang, Integritätsstatus, Dateiliste und Datenbestands-Hash |
| `README.txt` | Kurzanleitung im Exportpaket |
| `schema/field_catalog.json` | Vollständiger technischer Feldkatalog aller exportierten Tabellen |
| `data/*.json` | Fach- und Stammdaten; Listen oder Einzelobjekte gemäß Feldkatalog |
| `data/account_history.json` | Verkettete Kontenstamm-Historie mit Vorher-/Nachher-Snapshots |
| `documents/*` | Optional eingebettete Originalbelege |

Der Feldkatalog beschreibt je Datei die JSON-Form, Quelltabelle, fachliche
Kurzbeschreibung sowie für jedes Feld JSON-Typ, SQL-Typ, Nullbarkeit,
Primärschlüsseleigenschaft, Fremdschlüssel und eine deutsche Beschreibung.

## 3. Exportumfang und Filter

`date_from` und `date_to` begrenzen datierte Bewegungsdaten wie Buchungen,
Bankumsätze, offene Posten, Abschreibungen und Abrechnungsläufe. Stammdaten und
notwendige Hilfsdaten werden gesellschaftsweit ausgegeben. Das Manifest hält
diese Regel unter `scope_notes` ausdrücklich fest; es gibt keine stillen Filter.

Der Audit-Log-Export ist auf die Gesellschaft gefiltert. Die im Manifest
enthaltene Audit-Integritätsprüfung bewertet dagegen die vollständige
Mandanten-Hashkette zum Exportzeitpunkt, weil einzelne Gesellschaftseinträge in
der mandantenweiten Kette nicht zwingend zusammenhängend sind.

Kontenanlagen und Änderungen werden zusätzlich als eigener Prüfdatenbestand
`account_history.json` ausgegeben. Jeder Eintrag enthält den vollständigen
Konten-Snapshot unter `payload.before` und `payload.after`, Benutzer, Zeitpunkt,
Sequenznummer sowie die Hashverkettung. Kontonummer und Kontotyp sind nach der
Anlage strukturell unveränderbar; Änderungen sind auf Bezeichnung und
Aktivstatus begrenzt.

Mandantenbezogene Benutzer werden mit Benutzername, Rolle, Aktivstatus und der
Information exportiert, ob ein API-Token konfiguriert ist. Passwort-Hashes,
Token-Hashes und Token-Endungen werden ausdrücklich nicht exportiert. Beim
Belegindex wird der interne Speicherpfad ebenfalls nicht ausgegeben.

## 4. Prüfsummen und Reproduzierbarkeit

Jeder Eintrag unter `files` enthält:

- relativen Paketpfad,
- Dateigröße in Bytes,
- SHA-256-Hash des exakten Dateiinhalts.

`dataset_sha256.value` ist der SHA-256-Hash über die kanonisch nach Pfad
sortierte Liste aus Pfad, Größe und Datei-Hash. Er bleibt bei identischem
Datenstand und gleichen Exportparametern stabil, auch wenn der Export zu einem
anderen Zeitpunkt erneut erzeugt wird. Bei identischem `generated_at` ist auch
das ZIP byteidentisch, weil Zeitstempel und Reihenfolge der ZIP-Einträge
deterministisch gesetzt werden.

Der SHA-256-Hash des vollständigen ZIPs kann nicht in das ZIP selbst eingebettet
werden, ohne einen Zirkelschluss zu erzeugen. Er wird deshalb beim Download im
HTTP-Header `X-OpenBuchhaltung-SHA256`, bei `manifest_only=true` zusätzlich im
JSON-Feld `package_sha256` und durch die lokale Prüfung ausgegeben.

## 5. Lokale Prüfung

```bash
flask --app run.py verify-audit-package prueferexport-company-1-2026-07-13.zip
```

Die Prüfung kontrolliert ZIP-Struktur, doppelte oder unerwartete Pfade, alle
Dateigrößen und Einzelhashes sowie den Datenbestands-Hash. Abweichungen führen zu
einem Fehlerstatus und einer pfadbezogenen Diagnose. Der Nachweis schützt vor
unbemerkter Veränderung, ist aber keine externe digitale Signatur.

## 6. Belegindex

Der Belegindex enthält das fachliche `document_date` getrennt vom technischen
`uploaded_at`, die Buchungsverknüpfung, gespeicherte Hash- und
Versionsinformationen sowie die beim Export erneut ermittelten
Dateieigenschaften. Bei vor Einführung des Belegdatums angelegten Altdaten kann
`document_date` `null` sein; es wird bewusst kein historisches Datum erfunden.
`file_included` zeigt, ob die Originaldatei eingebettet wurde. Bei bewusst
deaktivierter Belegeinbettung sind die Prüfwerte `null`; bei angeforderter, aber
fehlender Datei ist `file_missing=true` und die gemeinsame Integritätsprüfung
schlägt fehl.
