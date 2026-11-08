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

6. **Stichtagsbewertung zum Stand des Stichtags, als Buchung mit Auflösung am Folgetag:** Fremdbetrag und Buchwert
   werden aus den Ausgleichen zurückgerechnet, eine spätere Zahlung ändert den Stichtag nicht. Gewinne und Verluste
   eines Partners bleiben getrennt (Einzelbewertung). Die Posten behalten ihren Buchwert, die Differenz bei
   der Zahlung entsteht gegen den ursprünglichen Kurs. Bei Restlaufzeit über einem Jahr wird nach § 256a HGB nur ein
   Verlust gebucht.
7. **Rechnung in Fremdwährung:** Forderung und Umsatzsteuer entsprechen genau dem Kurs; eine Rundungsdifferenz aus der
   Einzelumrechnung liegt auf dem größten Erlös. PDF und XRechnung nennen Kurs und Umsatzsteuer in Euro. Der
   Rechnungskurs folgt dem Leistungsdatum. Die Steuerzeile trägt ihren Fremdbetrag, damit die Steuer zum Monatskurs
   auf den Cent genau rechnet.
8. **Umsatzsteuer bei Istversteuerung zum Monatskurs des Geldeingangs** (§ 16 Abs. 6 Satz 2 UStG; die Fachlogik steht
   in `_foreign_paid_entry` und `_post_vat_fx_difference`).

   **UStVA:** Die Rechnung bucht weiter zum Rechnungskurs. Die UStVA rechnet je Zahlung: Der Teil der Fremdbeträge, den
   sie deckt, wird zum Monatskurs umgerechnet. Der Meldezeitraum folgt dem Datum des Ausgleichs, der Kurs dem
   Geldeingang (`received_on`): dem Buchungstag des Bankumsatzes, ohne Bankumsatz dem genannten Datum oder dem der
   Buchung. Wird ein Umsatz nach einem Storno neu zugeordnet, rückt das Datum hinter den Storno, und wenn der
   Monat gesperrt ist, läuft die Neuzuordnung über eine neue Buchung; das Geld kam aber im ursprünglichen Monat. Der
   Storno einer Zahlung nimmt im Zeitraum des Stornos genau den
   gemeldeten Betrag zurück, nicht den Betrag zum Kurs dieses Monats. Damit sich die Teile einer Rechnung ohne
   Rundungsrest summieren, zieht jede Zahlung die Teile ab, die bei ihrer Erfassung standen; das hängt an der
   tatsächlichen Reihenfolge, nicht an den Daten, und ändert sich durch spätere Vorgänge nicht.

   **Hauptbuch:** Beim Festhalten eines Zeitraums bucht das System die Differenz zwischen gebuchter und gemeldeter
   Steuer je Steuerkonto gegen „Erträge“ bzw. „Aufwendungen aus Kursdifferenzen“ (Quelle `vat_fx_difference`), datiert
   auf das Ende des Zeitraums oder in den nächsten offenen. Das Steuerkonto zeigt danach die gemeldete Steuer plus
   den noch nicht fälligen Teil zum Rechnungskurs. Die Kursdifferenz der Zahlung entsteht auf das Brutto; mit dieser
   Buchung bleibt im Erfolg nur die Differenz auf das Entgelt. Beispiel: 1.190 USD brutto, 1.000 USD Entgelt,
   Rechnungskurs 1,25, Monats- und Bankkurs 1,20. Geldeingang 991,67 € = Erlös 800,00 € + Steuer 158,33 € + Kursgewinn
   33,34 € (39,67 € aus der Zahlung, 6,33 € zurück aus dieser Buchung). Die Erlöse bleiben zum Rechnungskurs; die
   Bemessungsgrundlage der UStVA weicht also von den Erlöskonten ab. Jede Zahlung und jeder Storno wird einmal
   gebucht, vermerkt am Ausgleich; auch die Jahreserklärung bucht nur, was noch fehlt. Die Buchung zählt nie in der
   UStVA und lässt sich nicht einzeln stornieren oder ergänzen. Fehlt ein Konto für Kursdifferenzen, wird nicht
   festgehalten.

   **Gemeldete Zeiträume ändern sich nicht:** Beim Festhalten steht der Kurs an der Zahlung (`vat_rate`). Ein später
   erfasster oder korrigierter Monatskurs ändert weder die Neuberechnung des Zeitraums noch den Betrag eines späteren
   Stornos, und der festgehaltene Snapshot bleibt ohnehin. Die Migration setzt schon gemeldete Zahlungen und
   gemeldete Stornos auf den Rechnungskurs, zu dem sie gemeldet wurden.

   Auch ein Storno oder eine Neuzuordnung ändert keinen gemeldeten Zeitraum, in Euro wie in Fremdwährung. Dafür hat der
   Ausgleich eigene Tage für die UStVA (`vat_settled_on`, `vat_reversed_on`); `settlement_date` und `reversed_on` bleiben
   die echten Tage, nach denen sich etwa die Stichtagsbewertung richtet. Läge der Storno einer gemeldeten Zahlung in
   einem festgehaltenen Zeitraum, zählt er am ersten Tag dahinter, ebenso die Neuzuordnung derselben Zahlung. Fehlt die
   Zahlung in der Meldung ihres Zeitraums, weil sie erst danach zugeordnet wurde, gibt es nichts zu korrigieren: Ihr
   Storno hebt sie an ihrem Tag auf, und in keinem anderen Zeitraum entsteht eine Minderung. Eine andere Zahlung
   behält ihr Datum; ist sie erst nach der Meldung zugeordnet, nennt die UStVA sie weiter (Entscheidung 5), ihre
   Differenz bucht spätestens die Jahreserklärung.

   Eine Direktbuchung als Rechnung, die nach Ist zählt, lässt sich nicht stornieren, solange eine gebuchte Zahlung auf
   ihr steht: erst die Zahlung. Ein Ausgleich ohne eigene Buchung fällt mit der Rechnung, ebenso die Zahlung auf
   derselben Buchung, und ihr Posten schließt. Ausgangsrechnungen haben ihren eigenen Stornoweg (ADR-006). Der Wechsel
   zurück zur Sollversteuerung ist nicht möglich, wenn für Rechnungen ab diesem Datum eine gebuchte Differenz stehen
   bliebe. Festhalten, Ausgleich und Storno laufen bei Istversteuerung je Gesellschaft nacheinander, sonst ginge eine
   gleichzeitige Zahlung in keiner Meldung auf.

   Euro und Fremdwährung rechnen gleich: Jede Zahlung deckt einen festen Teil der Rechnung, unabhängig von später
   erfassten oder rückdatierten Zahlungen. Bisher rechnete der Euro-Pfad kumuliert nach Datum; eine rückdatierte
   Zahlung verschob dann den Stand vor einem festgehaltenen Monat, und die Deckelung auf 100 % verschluckte einen Storno.

   **Fehlender Kurs:** Die Vorschau rechnet vorläufig zum Rechnungskurs und nennt Währung und Monat. Festhalten wird
   abgewiesen, wie bei jedem fehlenden Kurs (Entscheidung 3). Der Monatskurs erscheint Anfang des Folgemonats, also
   vor der Abgabefrist; für Währungen ohne BMF-Kurs wird er von Hand erfasst.

   **Abgrenzung:** Das gilt für Rechnungen mit verknüpftem Posten in einer Währung und zu einem Kurs. Liegen auf einer
   Buchung Posten in verschiedenen Währungen oder zu verschiedenen Kursen, zählt sie zum Rechnungskurs und die UStVA
   warnt. Im DATEV-Stapel läuft die Differenzbuchung ohne Steuerschlüssel. Eine Rechnung ohne Umsatzsteuer bleibt
   nach ADR-009 im Rechnungsmonat; die steuerfreie Zeile einer gemischten Rechnung folgt der Zahlung.

   **Fachliche Bestätigung:** Gegenkonto, Zeitpunkt der Buchung und der Kurs nach einem Storno sind eigene Entscheidungen
   ohne ausdrückliche Vorgabe in § 16 Abs. 6 UStG oder Abschnitt 16.4 UStAE. Ein Steuerberater bestätigt sie vor dem
   ersten Produktiveinsatz, dazu die Frage, ob bei einem niedrigeren Betrag zum Monatskurs der höhere Steuerbetrag der
   Rechnung nach § 14c UStG geschuldet bleibt.
9. **Monatskurse in eigener Tabelle `vat_exchange_rate`,** Quelle `bmf` oder `manual`, je Mandant wie die Tageskurse.
   In `exchange_rate` träfe ein Monatskurs am Monatsersten auf den Tageskurs, und Rechnungen, Zahlungen und
   Stichtagsbewertung läsen ihn als Tageskurs; dort gilt aber der Devisenkurs, nicht der Steuerkurs. Quelle ist die
   Jahresdatei im Datenportal des BMF (CSV, Lizenz Datenlizenz Deutschland – Namensnennung 2.0). Ohne Jahr ruft
   der Abruf das laufende und das Vorjahr ab, weil der Dezemberkurs erst im Januar erscheint. Manuelle Kurse bleiben,
   eine Korrektur des BMF wird übernommen. Die Spaltenköpfe wechseln je Jahr, die Datei wird anhand von Monatsnamen
   und Titeljahr geprüft. Eine Zelle, deren Währungscode nicht zum Rest der Zeile passt (2021/2022: „RW“ statt „KRW“),
   wird nicht geraten, sondern gemeldet.

## Konsequenzen
- Positiv: Kein Bericht und keine Steuerauswertung muss Währungen kennen. Bestehende Daten bleiben unverändert.
- Negativ: Eine Bankzeile kennt den Fremdbetrag, aber der Saldenabgleich kann Fremdwährungsumsätze nicht gegen den
  Buchsaldo summieren und lässt sie aus. Bestehende Gesellschaften müssen die Konten für Kursdifferenzen anlegen.
- Negativ: Eine Zahlung nur in Euro braucht zwei Angaben. Das ist Absicht, kostet aber einen Handgriff.
- Negativ: Beim Festhalten entsteht bei Istversteuerung in Fremdwährung eine Buchung. Das ist eine Ausnahme von ADR-009
  (Berechnung statt zusätzlicher Buchungen), aber der einzige Zeitpunkt, an dem der Betrag endgültig ist.
- Offene Punkte: Monatskurs bei der Sollversteuerung (§ 16 Abs. 6 Satz 1 UStG: Monat der Leistung) und für die Vorsteuer;
  beide rechnen weiter zum Kurs der Buchung. Tageskurs mit Gestattung des Finanzamts (Satz 3) und Vormonatskurs (Abschnitt
  16.4 Abs. 2 UStAE). Korrektur einer gebuchten Stichtagsbewertung, Bewertung von Bankkonten in Fremdwährung, Fremdwährung
  im DATEV-Stapel (Felder WKZ, Kurs, Basis-Umsatz), Erfassung in der Buchungsmaske, Eingangsrechnungen aus E-Rechnung und
  OCR in Fremdwährung (E-Rechnungen werden abgewiesen).

## Alternativen
- **Hauptbuch in mehreren Währungen:** Jeder Bericht müsste umrechnen, der Abschluss ist ohnehin in Euro. Verworfen.
- **Fremdbetrag in eigener Tabelle außerhalb des Hashs:** Die Angabe wäre nach der Festschreibung änderbar. Verworfen.
- **Umbewertung des Postens zum Stichtag:** Die Differenz bei Zahlung würde zweigeteilt und ließe sich schlechter
  prüfen. Verworfen zugunsten der Auflösung am Folgetag.
- **Steuerdifferenz nur in der UStVA, nicht im Hauptbuch:** Das Steuerkonto ginge nie auf und wiche dauerhaft von den
  Meldungen ab, die Differenz bliebe am Jahresende als ungeklärter Saldo. Verworfen.
- **Differenz bei jeder Zahlung buchen:** Der Monatskurs ist bei der Zahlung meist noch nicht veröffentlicht. Die Buchung
  müsste geschätzt und korrigiert werden. Verworfen.
- **Rechnung auf den Monatskurs umbuchen:** Ändert Erlös und festgeschriebene Buchungen und passt bei Teilzahlungen in
  mehreren Monaten nicht zu einem Betrag. Verworfen.
- **Festhalten ohne Monatskurs zum Rechnungskurs erlauben:** Der Zeitraum wäre gemeldet und falsch; ein nachgereichter
  Kurs dürfte ihn nicht mehr ändern. Verworfen.
