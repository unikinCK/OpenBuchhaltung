# ADR-009: Istversteuerung über Zahlungen je offenem Posten

- **Status:** Angenommen
- **Datum:** 2026-11-03
- **Entscheider:** OpenBuchhaltung Kernteam

## Kontext
Die UStVA leitet ihre Kennzahlen aus Buchungszeilen ab und ordnet sie nach Buchungs- bzw.
Leistungsdatum zu. Das entspricht der Sollversteuerung (§ 16 UStG). Bei der Istversteuerung (§ 20 UStG)
entsteht die Umsatzsteuer erst mit der Zahlung. Eine Rechnung kann in Teilen bezahlt werden, und
Zahlungen können storniert werden. Am offenen Posten stand bisher nur der letzte Ausgleich, ein Datum je
Zahlung fehlte.

## Entscheidung
1. **Eigene Tabelle `open_item_settlement`.** Jeder Ausgleich ist eine Zeile mit Datum, Betrag, Buchung und
   Bankumsatz. Das Datum kommt aus der Ausgleichsbuchung, sonst aus dem Bankumsatz, sonst ist es
   heute. Ein Storno der Ausgleichsbuchung löscht die Zeile nicht, sondern trägt das Stornodatum ein.
2. **Berechnung statt zusätzlicher Buchungen.** Im Hauptbuch wird weiter gebucht wie bei der
   Sollversteuerung. Nur die UStVA rechnet anders: Eine Rechnung mit Umsatzsteuer und verknüpftem offenen
   Posten fällt aus der Zuordnung nach Datum heraus; stattdessen zählen ihre Zeilen, skaliert auf den im
   Meldezeitraum bezahlten Anteil. Die Skalierung läuft über den kumulierten Anteil, sodass sich
   Teilzahlungen ohne Rundungsrest zur Rechnung summieren. Die übrige Auswertung (Kennzahlen, Steuersätze)
   bleibt dieselbe.
3. **Stornierte Zahlungen** mindern im Zeitraum des Stornos. So bleibt eine bereits gemeldete Voranmeldung
   richtig, und die Korrektur erscheint dort, wo sie entsteht.
4. **Abgrenzung:** Vorsteuer (§ 15 UStG), steuerfreie und nicht steuerbare Umsätze und direkt bezahlte
   Umsätze ändern sich nicht. Rechnungen von vor dem Beginn der Istversteuerung bleiben nach
   Buchungsdatum versteuert. Rechnungen auf einem Forderungskonto ohne verknüpften Posten zählen mit dem
   Buchungsdatum, die UStVA warnt.
5. **Zeitraum am Unternehmen:** `vat_cash_basis_from` und `vat_cash_basis_until`, Wechsel nur zum
   Monatsersten, ein zusammenhängender Zeitraum, Audit-Eintrag. Im KI-Chat gesperrt, weil der Wechsel die
   Gestattung des Finanzamts braucht.

## Konsequenzen
- Positiv: Keine zusätzlichen Buchungen, kein Eingriff in den Festschreibungs-Hash. Der Wechsel wirkt auch
  rückwirkend auf noch nicht gemeldete Zeiträume; gespeicherte Voranmeldungen bleiben wie festgehalten.
  Die Zahlungstabelle ist zugleich die Grundlage für Kursdifferenzen. Der Storno stellt jetzt alle
  Ausgleiche einer Buchung wieder her, nicht nur den letzten.
- Ausnahme: Bei Rechnungen in Fremdwährung gleicht das Festhalten das Steuerkonto an die zum Monatskurs gemeldete Steuer
  an (ADR-010, Entscheidung 8).
- Negativ: Das Steuerkonto enthält auch noch nicht fällige Umsatzsteuer. Sein Saldo weicht deshalb von der
  Zahllast ab; die UStVA nennt den Betrag als Warnung. Wer „Umsatzsteuer nicht fällig“ ausweisen möchte, bucht
  zum Abschluss um.
- Offene Punkte: Umgliederung auf „Umsatzsteuer nicht fällig“, mehrere Ist-Zeiträume je Gesellschaft,
  Kennzeichnung im DATEV-Export.

## Alternativen
- **Umgliederungsbuchung bei jeder Zahlung** (Konto „Umsatzsteuer nicht fällig“ an „Umsatzsteuer“): im
  Hauptbuch sauberer, aber Stornos, Teilzahlungen und festgeschriebene Zeiträume hätten jeweils eigene
  Folgebuchungen gebraucht, und Bestandsbuchungen auf den üblichen Steuerkonten wären falsch ausgewertet
  worden.
- **Partnerzahlungen statt Posten als Grundlage:** ohne Verknüpfung zur Rechnung ist der Steuersatz einer
  Zahlung nicht bestimmbar.
