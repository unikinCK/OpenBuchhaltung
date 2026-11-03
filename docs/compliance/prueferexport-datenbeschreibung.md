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
| `data/controlling_units.json` | Kostenstellen- und Profitcenter-Stammdaten |
| `data/controlling_unit_history.json` | Verkettete Vorher-/Nachher-Historie der Controlling-Einheiten |
| `data/business_partners.json` | Geschäftspartner (Debitoren/Kreditoren) mit Personenkontonummern |
| `data/business_partner_history.json` | Verkettete Vorher-/Nachher-Historie der Geschäftspartner inklusive Bankdatenänderungen |
| `data/cash_books.json` | Kassen der Gesellschaft mit Kassenkonto, Status und Nummernkreis |
| `data/cash_entries.json` | Unveränderbare Kassenbuch-Einträge mit fortlaufender Nummer, Bestand, Buchung und Hashkette |
| `data/cash_closings.json` | Unveränderbare Tagesabschlüsse (Kassensturz) mit Soll-Bestand, gezähltem Bestand und Differenz |
| `data/consolidation_groups.json` | Konsolidierungsgruppen, deren Muttergesellschaft die exportierte Gesellschaft ist |
| `data/consolidation_members.json` | Mitglieder dieser Gruppen mit Beteiligungsquote und Einbeziehungszeitraum |
| `data/consolidation_adjustments.json` | Manuelle Konsolidierungsbuchungen dieser Gruppen |
| `data/consolidation_adjustment_lines.json` | Zeilen der manuellen Konsolidierungsbuchungen |
| `data/consolidation_history.json` | Verkettete Vorher-/Nachher-Historie der Gruppen und Buchungen einschließlich gelöschter Buchungen |
| `data/open_item_settlements.json` | Ausgleiche offener Posten mit Zahlungsdatum, Buchung und Storno |
| `data/sales_invoices.json` | Ausgestellte und stornierte Ausgangsrechnungen sowie Stornorechnungen (ohne Entwürfe) mit Empfänger-Snapshot, Summen und Verweisen auf Buchung, offenen Posten und Belege |
| `data/sales_invoice_lines.json` | Positionen dieser Rechnungen mit Menge und Einzelpreis (vier Nachkommastellen) und Steuersatz |
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

Kostenstellen und Profitcenter werden mit Typ, Code, Hierarchie,
Gültigkeitszeitraum und Aktivstatus exportiert. Die Journalzeilen enthalten die
optionalen Schlüssel `cost_center_id` und `profit_center_id`; Stammdatenänderungen
stehen zusätzlich mit Audit-Hashes in `controlling_unit_history.json`. Bei
festgeschriebenen Buchungen sind die Zuordnungen Bestandteil des versionierten
Buchungsinhaltshashs.

Geschäftspartner werden mit Debitoren- bzw. Kreditorennummer, Art, Anschrift,
USt-IdNr, Steuernummer, Kontakt, Bankverbindung, Zahlungsziel und Aktivstatus
exportiert. Journalzeilen auf Sammelkonten (Kennzeichen `subledger` im Kontenstamm)
tragen den optionalen Schlüssel `partner_id`. Festgeschriebene Buchungen ab
Inhaltshash-Version 3 versiegeln diese Zuordnung; Buchungen, die vor Einführung des
Nebenbuchs festgeschrieben wurden, behalten ihr Siegel der Version 2. Buchungen
tragen optional das Leistungsdatum `service_date` (Umsatzsteuer-Meldezeitraum); ab
Inhaltshash-Version 4 ist es Teil des Siegels, ältere Siegel behalten ihre Version.
Nachträge vor der Festschreibung (Leistungsdatum, Partner) stehen als Aktion
`amended` mit altem und neuem Wert im Audit-Log. Anlage,
Änderungen und Bankdatenänderungen (`bank_details_changed`) stehen mit
Audit-Hashes in `business_partner_history.json`.

Das Kassenbuch steht in `cash_books.json`, `cash_entries.json` und
`cash_closings.json`. Einträge tragen eine je Kasse lückenlose Nummer, Art,
Richtung, Betrag, Bestand nach dem Eintrag, Verweise auf Buchung, Gegenkonto,
Steuercode, Partner, Beleg und Storno sowie die Hashkette `previous_hash` und
`content_hash` (SHA-256 über die kanonischen Felder einschließlich des
Vorgänger-Hashs, Verfahren in `hash_version`). Einträge und Tagesabschlüsse sind
per Datenbank-Trigger unveränderbar. `date_from`/`date_to` filtern Einträge nach
`entry_date` und Abschlüsse nach `closing_date`; die Kette lässt sich deshalb nur
in einem ungefilterten Export von Anfang an nachrechnen. Das Kassenbuch ist manuell
geführt und ersetzt keine TSE und keinen DSFinV-K-Export.
Konsolidierungsgruppen (ADR-008) gehören zum Paket ihrer Muttergesellschaft; der
Export einer Tochtergesellschaft enthält sie nicht. Manuelle Konsolidierungsbuchungen
sind keine Buchungen einer Gesellschaft und stehen nicht im Journal; sie werden nach
`date_from`/`date_to` gefiltert. Gelöschte Buchungen sind mit vollständigem Inhalt in
`consolidation_history.json` nachvollziehbar. `business_partners.json` enthält mit
`intercompany_company_id` die verbundene Gesellschaft, für die ein Partner steht.

Mandantenbezogene Benutzer werden mit Benutzername, Rolle, Aktivstatus und der
Information exportiert, ob ein API-Token konfiguriert ist. Passwort-Hashes,
Token-Hashes und Token-Endungen werden ausdrücklich nicht exportiert, ebenso wenig
die Daten der Zwei-Faktor-Anmeldung (Secret, Zeitstempel, Wiederherstellungscodes). Beim
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

`capture_source` nennt den Erfassungsweg (`web`, `api`, `einvoice`, `mobile_scan`,
`mobile_share`). Bei mobiler Erfassung stehen `captured_at` (Aufnahmezeitpunkt auf
dem Gerät, UTC) und `capture_device` daneben. Vor Einführung dieser Felder erfasste
Belege haben keinen Erfassungsweg; auch hier wird nichts nachträglich ergänzt.
