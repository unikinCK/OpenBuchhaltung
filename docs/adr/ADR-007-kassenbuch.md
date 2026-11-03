# ADR-007: Kassenbuch

- **Status:** Angenommen
- **Datum:** 2026-11-03
- **Entscheider:** OpenBuchhaltung Kernteam
- **Umsetzung:** `app/services/cash_book.py`, `app/api/cash_book.py`, `app/web/cash_book.py`,
  Migration `20261103_0059`; `docs/umsetzungsplan.md`, Phase 5

## Kontext
Das Projektreview (`docs/review/projektreview-2026-09-19.md`, Feature #9) und die Roadmap
versprechen ein Kassenbuch. Bareinnahmen und -ausgaben sind täglich aufzuzeichnen (§ 146 AO),
das Kassenbuch muss fortlaufend und unveränderbar sein, und ein rechnerischer Kassenfehlbetrag
ist bei einer Betriebsprüfung ein schwerer Mangel. Bisher ließ sich eine Kasse nur als Konto
führen: Das Journal kennt weder Bestand je Eintrag noch Tagesabschluss, und nichts verhindert
einen negativen Bestand.

## Entscheidung

### 1. Eigene Tabellen, Buchung als Folge
`cash_book` (Kasse, gebunden an genau ein Aktivkonto), `cash_entry` (Eintrag) und `cash_closing`
(Tagesabschluss). Das Kassenbuch führt, das Hauptbuch folgt: Jeder Eintrag erzeugt in derselben
Transaktion genau eine Buchung (`source = cash_book`). Schlägt die Buchung fehl (gesperrte Periode,
abgeschlossenes Geschäftsjahr, unpassender Steuercode), rollt alles zurück, auch der Nummernkreis.
Die Steuer entsteht wie bei der Bankbuchung aus dem Bruttobetrag (`net_from_gross`, der Rundungscent
liegt auf der Steuerzeile).

Eine Kasse braucht ein Aktivkonto ohne Sammelkonto-Kennzeichen; ein Konto mit Bankumsätzen wird
abgelehnt. Je Gesellschaft sind Konto und Name eindeutig. Das Konto ist nach der Anlage fest.

### 2. Anfangsbestand: beide Wege, aber nur stimmig
Der Anfangsbestand ist der erste Eintrag (Art `opening`, zählt im Kassenbericht nicht als Einnahme).
- **Mit Gegenkonto** (z. B. Saldenvorträge): wird gebucht. Das Konto darf am Stichtag noch keinen
  Saldo haben, sonst wäre der Bestand doppelt gebucht.
- **Ohne Gegenkonto:** keine Buchung, `journal_entry_id` und Gegenkonto bleiben leer. Der Betrag muss
  dem Kontensaldo am Stichtag entsprechen; ohne Betrag gilt der Kontensaldo. Das ist der übliche Weg,
  wenn die Eröffnungsbilanz das Kassenkonto schon bebucht hat.

So stimmen Kassenbestand und Kontensaldo vom ersten Tag an. Weicht die gezählte Kasse ab, gehört das
in den ersten Kassensturz und nicht in einen frei gewählten Anfangsbestand. Bei Bestand 0 entsteht
kein Eintrag (Betrag > 0 gilt für alle Einträge); die Kette beginnt dann mit dem ersten Vorgang. Ein
Anfangsbestand ohne Buchung lässt sich nicht stornieren, einer mit Buchung schon.

### 3. Betrag immer positiv, Richtung als eigene Spalte
Differenz- und Storno-Einträge können den Bestand heben oder senken, die Art allein reicht also nicht.
`direction` (`in`, `out`) steht deshalb in der Tabelle und im Hash, statt die Richtung aus dem Bestand
abzuleiten.

### 4. Unveränderbar auf drei Ebenen
- **DB-Trigger** (SQLite und PostgreSQL) lehnen UPDATE und DELETE auf `cash_entry` und `cash_closing` ab.
- **Hashkette je Kasse:** `content_hash` ist SHA-256 über die kanonischen Felder einschließlich
  `previous_hash`, der erste Eintrag verkettet gegen 64 Nullen. `hash_version` hält das Verfahren fest.
- **Nummernkreis:** `cash_book.last_entry_number`, die Zeile wird beim Erfassen mit `FOR UPDATE`
  gesperrt. Damit sind Nummer, Vorgänger-Hash und Bestand je Kasse serialisiert; ein Rollback nimmt den
  Zähler mit zurück, die Nummern bleiben lückenlos. Die UNIQUE-Constraints fangen den Wettlauf auf
  SQLite ab, wo `FOR UPDATE` nichts tut.

### 5. Storno nur im Kassenbuch
Ein Storno-Eintrag spiegelt den Eintrag am aktuellen Datum und storniert die Buchung über
`reverse_journal_entry` (jetzt mit `commit=False`, damit beides eine Transaktion ist). Ein Eintrag
lässt sich höchstens einmal stornieren (UNIQUE auf `reversal_of_id`), ein Storno gar nicht.
`reverse_journal_entry` lehnt die Buchung eines Kassenbuch-Eintrags außerhalb des Kassenbuchs ab, sonst
liefen Bestand und Konto auseinander. Auch ein Storno darf den Bestand nicht negativ machen und nicht in
einem abgeschlossenen Tag liegen: Nach dem heutigen Kassensturz geht die Korrektur erst morgen. Der
Beleg eines stornierten Eintrags wird frei, damit die Neuerfassung ihn nutzen kann (eine Buchung hält
den Beleg nur einzeln); Eintrag und Storno behalten den Verweis, und das Audit-Log hält den Wechsel fest.

### 6. Buchung bleibt bis zur regulären Festschreibung offen
Die Buchung eines Eintrags wird nicht sofort festgeschrieben, sondern folgt der normalen Festschreibung
wie jede Bankbuchung. Das Kassenbuch braucht das Siegel nicht: `amend_journal_entry` ändert weder Betrag
noch Konto noch Datum, Bestand und Prüfung bleiben also stimmig, und Kostenstelle, Profitcenter sowie
Leistungsdatum lassen sich weiterhin ergänzen. Der Eintrag selbst bleibt dabei unverändert; er hält fest,
was an der Kasse erfasst wurde.

### 7. Tagesabschluss sperrt
Ein Kassensturz hält Soll-Bestand, gezählten Bestand und Differenz fest und sperrt alle Tage bis zu seinem
Datum. Er darf nicht vor dem letzten Eintrag liegen, nicht in der Zukunft und nicht in einem bereits
abgeschlossenen Tag. Eine Differenz wird als Eintrag der Art `difference` gegen ein vom Benutzer gewähltes
Konto gebucht, ohne Steuer. Der Soll-Bestand zählt die Differenz nicht mit.

### 8. Regeln beim Erfassen
Datum nicht in der Zukunft, nicht vor dem letzten Eintrag, nicht in einem abgeschlossenen Tag; Bestand nie
negativ (die Meldung erklärt, warum); inaktive Kasse ohne neue Einträge. „Heute“ ist die deutsche Ortszeit,
weil der Server oft in UTC läuft und eine Kasse sonst nachts Einträge als „Zukunft“ ablehnen würde. Inaktiv
setzen lässt sich eine Kasse nur mit Bestand 0, damit kein Bargeld in einer stillgelegten Kasse liegt.

### 9. Prüfung meldet, blockiert aber nicht
`verify_cash_book_integrity` prüft Hashkette, lückenlose Nummern, Nummernkreis, Bestandsfortschreibung,
Chronologie, den Betrag jeder Buchung auf dem Kassenkonto und die Kassenstürze (`valid`). Die
Übereinstimmung mit dem Kontensaldo ist eine eigene Aussage (`account_matches`) und nur eine Warnung:
Buchungen am Kassenbuch vorbei sind ein Prüfthema, das Kassenbuch selbst bleibt intakt und benutzbar. Die
Prüfung nennt solche Buchungen einzeln. Saldovorträge zählen beim Kontensaldo nicht mit, weil sie nur die
Vorjahre wiederholen (wie beim Bank-Saldenabgleich). `verify_compliance_integrity` bleibt unberührt.

## Grenzen
Das ist ein **manuell geführtes** Kassenbuch. Es ersetzt **keine TSE** (§ 146a AO), **keinen DSFinV-K-Export**
für elektronische Kassensysteme und keine Kassenmeldung, und es ist **nicht zertifiziert**. Der Betrieb
entscheidet selbst, ob eine offene Ladenkasse für ihn zulässig ist; das Kassenbuch bildet weder das
Zählprotokoll je Stückelung noch die retrograde Ermittlung des Kassenberichts ab. Die Trigger und die
Hashkette machen Manipulation erkennbar, verhindern aber keinen Eingriff mit Datenbankrechten.

## Konsequenzen
- Positiv: fortlaufender Bestand je Tag, Kassenbericht, Kassensturz und Prüfung über UI, REST und MCP;
  Tabellen im Prüferexport; keine negativen Bestände.
- Negativ: `reverse_journal_entry` kennt nun das Kassenbuch (`commit`, `from_cash_book`). Tippfehler lassen
  sich nur per Storno und Neuerfassung korrigieren. Ein vergessener Tag lässt sich nicht nachtragen, sobald
  ein späterer Eintrag oder Abschluss existiert: Das Kassenbuch ist chronologisch.
- Offene Punkte: Kostenstelle und Profitcenter nur nachträglich an der offenen Buchung; kein Zählprotokoll
  je Stückelung; keine Anbindung elektronischer Kassen. Die Prüfung läuft auf Abruf und ist nicht Teil von
  `verify_compliance_integrity`.

## Alternativen
- **Kasse nur als Kontoblatt mit Bestandsprüfung:** verworfen, weil das Journal keine fortlaufende Nummer je
  Kasse, keinen Tagesabschluss und keine Sperre gegen Rückdatierung kennt.
- **Buchung sofort festschreiben:** verworfen (siehe 6). Der Eintrag ist die unveränderbare
  Grundaufzeichnung, und die Ergänzung von Kostenstellen wäre sonst nur noch per Storno möglich.
- **Storno aus dem Journal automatisch ins Kassenbuch spiegeln:** verworfen. Das Journal kennt weder
  Tagesabschluss noch Bestandsregel; eine Sperre mit klarer Meldung ist eindeutiger.
- **Anfangsbestand immer buchen:** verworfen, weil das Eröffnungsbilanz-Buchungen verdoppeln würde.
