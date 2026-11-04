# ADR-010: Fremdwährung als Zusatzangabe zur Buchhaltung in Euro

- **Status:** Angenommen
- **Datum:** 2026-11-04
- **Entscheider:** OpenBuchhaltung Kernteam

## Kontext
Die Buchhaltung kannte nur die Firmenwährung: `journal_entry_line.currency_code` war immer der Euro, Bankumsätze
in Fremdwährung wurden abgewiesen. Rechnungen und Zahlungen in US-Dollar oder Schweizer Franken sind aber Alltag.
Nach § 244 HGB wird der Jahresabschluss in Euro aufgestellt. Fremdwährungsposten sind zum Stichtag zu bewerten
(§ 256a HGB), die Umsatzsteuer wird in Euro geschuldet (§ 16 Abs. 6 UStG).

## Entscheidung
1. **Das Hauptbuch bleibt in Euro.** Soll und Haben sind immer Eurobeträge, alle Berichte rechnen unverändert.
   Fremdwährung, Fremdbetrag und Kurs stehen zusätzlich an der Buchungszeile. Sie sind Teil des
   Festschreibungs-Hashs (**Version 5**); ältere Siegel bleiben gültig, es gibt kein Re-Hashing.
2. **Kursnotierung der EZB:** Einheiten der Fremdwährung je 1 Euro. Deshalb setzt Fremdwährung den Euro als
   Firmenwährung voraus. Kurse gelten je Mandant, nicht je Instanz, damit ein Mandant keine Kurse eines anderen
   ändert.
3. **Ein fehlender Kurs ist ein Fehler.** Ohne Kurs gilt der letzte aus den zehn Tagen davor (Wochenenden,
   Feiertage). Danach wird abgewiesen, ein stiller Standardwert würde falsche Beträge buchen. Der Abruf der EZB
   überschreibt keine manuell erfassten Kurse.
4. **Offener Posten: Buchwert in Euro, Fremdbetrag daneben.** `original_amount` und `open_amount` bleiben der
   Buchwert, damit bestehende Auswertungen (Saldenliste, Istversteuerung) weiter stimmen. Beim Ausgleich geht der
   anteilige Buchwert heraus; die letzte Zahlung nimmt den Rest, damit kein Rundungscent stehen bleibt.
5. **Realisierte Kursdifferenz:** `open_item_settlement` hält den gezahlten Betrag und die Differenz fest. Mit einer
   Ausgleichsbuchung wird sie gegen das Sammelkonto gebucht, sodass dieses aufgeht. Ohne Ausgleichsbuchung gibt es
   nichts im Hauptbuch auszugleichen; dann bleibt es beim Festhalten. Der Storno der Zahlung nimmt die Differenz
   zurück. Die Istversteuerung rechnet weiter über den Buchwert, die Umsatzsteuer ändert sich durch den Kurs bei
   Zahlung also nicht.
6. **Stichtagsbewertung als Buchung mit Auflösung am Folgetag:** Die Posten behalten ihren Buchwert, die Differenz bei
   der Zahlung entsteht gegen den ursprünglichen Kurs. Bei Restlaufzeit über einem Jahr wird nach § 256a HGB nur ein
   Verlust gebucht.
7. **Rechnung in Fremdwährung:** Forderung und Umsatzsteuer entsprechen genau dem Kurs; eine Rundungsdifferenz aus der
   Einzelumrechnung liegt auf dem größten Erlös. PDF und XRechnung nennen Kurs und Umsatzsteuer in Euro.

## Konsequenzen
- Positiv: Kein Bericht und keine Steuerauswertung muss Währungen kennen. Bestehende Daten bleiben unverändert.
- Negativ: Eine Bankzeile kennt den Fremdbetrag, aber der Saldenabgleich kann Fremdwährungsumsätze nicht gegen den
  Buchsaldo summieren und lässt sie aus. Bestehende Gesellschaften müssen die Konten für Kursdifferenzen anlegen.
- Offene Punkte: BMF-Monatskurse (§ 16 Abs. 6 UStG; heute nur als manueller Kurs), Bewertung von Bankkonten in
  Fremdwährung, Fremdwährung im DATEV-Stapel (Felder WKZ, Kurs, Basis-Umsatz), Erfassung in der Buchungsmaske,
  Eingangsrechnungen aus E-Rechnung und OCR in Fremdwährung.

## Alternativen
- **Hauptbuch in mehreren Währungen:** Jeder Bericht müsste umrechnen, der Abschluss ist ohnehin in Euro. Verworfen.
- **Fremdbetrag in eigener Tabelle außerhalb des Hashs:** Die Angabe wäre nach der Festschreibung änderbar. Verworfen.
- **Umbewertung des Postens zum Stichtag:** Die Differenz bei Zahlung würde zweigeteilt und ließe sich schlechter
  prüfen. Verworfen zugunsten der Auflösung am Folgetag.
