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
5. **Realisierte Kursdifferenz:** `open_item_settlement` hält den gezahlten Betrag und die Differenz fest. Steht die
   Zahlung auf dem Sammelkonto, wird die Differenz dagegen gebucht, sodass dieses aufgeht. Sonst gibt es nichts im
   Hauptbuch auszugleichen; dann bleibt es beim Festhalten. Der Storno der Zahlung nimmt die Differenz zurück, die
   Differenzbuchung allein ist nicht stornierbar.

   **Nichts wird geraten:** Eine Zahlung nur in Euro sagt nicht, wie viel Fremdwährung sie deckt und ob sie mehrere
   Posten bezahlt; eine abgeleitete Differenz wäre erfunden. Deshalb nennt man dort Fremdbetrag und Eurobetrag selbst.
   Eine Buchung mit Zeilen in der Währung des Postens liefert beides, anteilig und ohne Rundungsrest. Zeilen auf dem
   Sammelkonto belegen nur Posten gleicher Währung und gleichen Partners; auf der Geldseite belegen alle Posten
   dasselbe Geld, andere Währungen anteilig. Ein Bankumsatz muss vor dem Ausgleich gebucht sein, sonst stünde die
   Zahlung nicht im Hauptbuch. Steht die Zahlung auf dem Sammelkonto, lässt sich einem Posten in Fremdwährung nicht
   mehr zuordnen, als sie dort bewegt hat. Ein Bankumsatz in Euro trägt alle Posten zusammen nur bis zu seinem Betrag,
   wohin er auch gebucht ist; jeder Ausgleich belegt seinen gezahlten Eurobetrag. Ein nicht zugeordneter Rest bleibt
   sichtbar stehen; das kann auch ein zu niedrig angegebener Kurs sein.

   **Datum:** Wird dieselbe Zahlung nach einem Storno neu zugeordnet, auch auf einen anderen Posten, liegt der neue
   Ausgleich nie vor dem Storno, sonst stünde sie rückwirkend doppelt in einem gemeldeten Zeitraum und am Stichtag.
   Eine andere Zahlung behält ihr Datum. Bei Istversteuerung warnt die UStVA, wenn eine Zahlung erst nach der Meldung
   ihres Zeitraums zugeordnet wurde. Die Differenzbuchung weicht aus einem gesperrten
   Monat oder abgeschlossenen Jahr in den nächsten offenen Zeitraum aus, das Zahlungsdatum bleibt.

   **Umsatzsteuer:** § 16 Abs. 6 UStG nimmt bei der Sollversteuerung den Monat der Leistung, bei der Istversteuerung
   den Monat der Zahlung. Der Rechnungskurs folgt dem Leistungsdatum. Die Istversteuerung rechnet weiter über den
   Buchwert und damit zum Kurs der Rechnung, weil das Steuerkonto bei der Rechnung gebucht ist. Die UStVA warnt bei
   bezahlten Rechnungen in Fremdwährung, die Korrektur bleibt manuell.
6. **Stichtagsbewertung zum Stand des Stichtags, als Buchung mit Auflösung am Folgetag:** Fremdbetrag und Buchwert
   werden aus den Ausgleichen zurückgerechnet, eine spätere Zahlung ändert den Stichtag nicht. Gewinne und Verluste
   eines Partners bleiben getrennt (Einzelbewertung). Die Posten behalten ihren Buchwert, die Differenz bei
   der Zahlung entsteht gegen den ursprünglichen Kurs. Bei Restlaufzeit über einem Jahr wird nach § 256a HGB nur ein
   Verlust gebucht.
7. **Rechnung in Fremdwährung:** Forderung und Umsatzsteuer entsprechen genau dem Kurs; eine Rundungsdifferenz aus der
   Einzelumrechnung liegt auf dem größten Erlös. PDF und XRechnung nennen Kurs und Umsatzsteuer in Euro.

## Konsequenzen
- Positiv: Kein Bericht und keine Steuerauswertung muss Währungen kennen. Bestehende Daten bleiben unverändert.
- Negativ: Eine Bankzeile kennt den Fremdbetrag, aber der Saldenabgleich kann Fremdwährungsumsätze nicht gegen den
  Buchsaldo summieren und lässt sie aus. Bestehende Gesellschaften müssen die Konten für Kursdifferenzen anlegen.
- Negativ: Eine Zahlung nur in Euro braucht zwei Angaben. Das ist Absicht, kostet aber einen Handgriff.
- Offene Punkte: BMF-Monatskurse (§ 16 Abs. 6 UStG; heute nur als manueller Kurs), Umsatzsteuer zum Kurs des
  Zahlungsmonats bei Istversteuerung (heute Warnung), Korrektur einer gebuchten Stichtagsbewertung, Bewertung von
  Bankkonten in Fremdwährung, Fremdwährung im DATEV-Stapel (Felder WKZ, Kurs, Basis-Umsatz), Erfassung in der
  Buchungsmaske, Eingangsrechnungen aus E-Rechnung und OCR in Fremdwährung (E-Rechnungen werden abgewiesen).

## Alternativen
- **Hauptbuch in mehreren Währungen:** Jeder Bericht müsste umrechnen, der Abschluss ist ohnehin in Euro. Verworfen.
- **Fremdbetrag in eigener Tabelle außerhalb des Hashs:** Die Angabe wäre nach der Festschreibung änderbar. Verworfen.
- **Umbewertung des Postens zum Stichtag:** Die Differenz bei Zahlung würde zweigeteilt und ließe sich schlechter
  prüfen. Verworfen zugunsten der Auflösung am Folgetag.
