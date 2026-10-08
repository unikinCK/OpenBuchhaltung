# ADR-002: Geschäftspartner als Nebenbuch an Sammelkonten

- **Status:** Angenommen
- **Datum:** 2026-10-08
- **Entscheider:** OpenBuchhaltung Kernteam
- **Detailplan:** `docs/sprint/sprint-5-geschaeftspartner-stammdaten.md`

## Kontext
Kunden und Lieferanten existierten nur als Freitext (`OpenItem.counterparty`,
`BankTransaction.counterparty`). Für offene Posten, Zahllauf, Mahnwesen, E-Rechnung
und den DATEV-Export braucht es einen Kunden-/Lieferantenstamm mit
Personenkontonummern (Projektreview 2026-09-19, Feature #1).

DATEV führt Debitoren (10000–69999) und Kreditoren (70000–99999) als eigene
Personenkonten. Diese Konten 1:1 als `Account`-Zeilen anzulegen, kollidiert mit
dem bestehenden Kontenmodell:

- `Account.derive_hierarchy` liest nur die ersten vier Ziffern; 10010 hinge unter
  1000 (Kasse), 70001 unter 7000.
- Die Summen-/Saldenliste sortiert Kontonummern als Text, die Bilanz zeigt eine
  Zeile je Konto statt einer Position „Forderungen aLuL“.
- Bankkonten-Auswahllisten zeigen Aktivkonten; jeder Debitor erschiene dort.
- Der Kontenplan bekäme Hunderte Konten, die Kontenpflege würde zur Partnerpflege.

## Entscheidung
1. **Neue Entität `BusinessPartner`** je Gesellschaft. Die Rolle ergibt sich aus der
   Personenkontonummer: Kunde = `debtor_number`, Lieferant = `creditor_number`
   (beides möglich). Nummern werden automatisch im DATEV-Bereich vergeben und sind
   nach der ersten Verwendung unveränderlich. Partner werden nicht gelöscht, nur
   deaktiviert. Änderungen werden mit Vorher-/Nachher-Snapshot in der verketteten
   Audit-Historie protokolliert; Bankdaten nur über einen eigenen Endpunkt mit
   eigener Aktion `bank_details_changed`.
2. **Nebenbuch statt Personenkonten:** Im Hauptbuch wird weiter auf die
   Sammelkonten gebucht. Konten tragen das Kennzeichen `subledger`
   (`debtor` nur auf `asset`-, `creditor` nur auf `liability`-Konten). Nur Zeilen auf
   solchen Konten dürfen `journal_entry_line.partner_id` tragen; die Rolle des
   Partners muss zur Seite passen. Die Personenkontonummer erscheint erst im
   DATEV-Export als Konto.
3. **Festschreibungs-Hash Version 3** nimmt `partner_id` je Zeile auf. Siegel der
   Version 2 bleiben unverändert gültig und prüfbar; es findet kein Re-Hashing
   statt. Die DB-Wächter akzeptieren beide Versionen.
4. **Saldovortrag je Partner:** Der Jahresabschluss trägt Sammelkonten je Konto und
   Partner vor, damit die Nebenbuchsalden im Folgejahr erhalten bleiben. Storno und
   Saldovortrag übernehmen Partner auch dann, wenn sie inzwischen deaktiviert sind.
5. **Bestandskonten** werden nicht per Migration gekennzeichnet; das Kennzeichen wird
   über die Kontenpflege mit Audit-Eintrag gesetzt. Der Kontenrahmen-Import erkennt
   Forderungen/Verbindlichkeiten aLuL an der Bezeichnung oder nimmt eine optionale
   Spalte `subledger`.

## Konsequenzen
- Positiv: Kontenplan, Berichte und Bilanzgliederung bleiben unverändert;
  Partnersalden sind über das Nebenbuch je Sammelkonto abstimmbar; die
  Festschreibung deckt die Partnerzuordnung ab.
- Negativ: Der DATEV-Export braucht ein Mapping Sammelkonto + Partner →
  Personenkonto; die Hash-Verifikation muss zwei Versionen unterstützen.
- Offene Punkte: OPOS/Zahllauf/Mahnwesen am Partner, Partnerkonto und Saldenliste,
  Partnererkennung in automatischen Quellen, DATEV-Personenkonten und
  Stammdatenexport (Schritte 3–7 des Detailplans).

## Alternativen
- **Personenkonten als `Account`-Zeilen:** verworfen wegen der oben genannten
  Kollisionen mit Hierarchie, Berichten und Bankkontenauswahl.
- **Partner am Buchungskopf statt an der Zeile:** verworfen, weil DATEV je Zeile auf
  Personenkonten bucht und Sammelzahlungen mehrere Partner betreffen können.
- **Re-Hashing aller Siegel auf Version 3:** verworfen; bestehende Siegel sollen
  nicht nachträglich verändert werden.
