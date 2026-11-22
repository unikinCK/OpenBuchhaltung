# ADR-012: Zahlungsavise – die KI liest, das Programm rechnet

- **Status:** Angenommen
- **Datum:** 2026-11-22
- **Entscheider:** OpenBuchhaltung Kernteam

## Kontext
Große Kunden und Zentralregulierer bezahlen viele Rechnungen in einer Summe und schicken dazu ein
Zahlungsavis: eine Liste der bezahlten Rechnungen mit ihren Abzügen – Skonto, Delkredere bzw.
Dienstleistungsvergütung, Bonus, Werbekostenzuschuss, Konventionalstrafe, Reklamationen, Gebühren.
Die Formate unterscheiden sich je Kunde und Sprache (Spaltenaufbau, Vorzeichen, Abzüge je Zeile oder nur
auf die Summe, Überträge je Seite). Bisher musste die Buchhaltung jede Rechnungsnummer im OPOS suchen,
die Differenz von Hand zerlegen und den Bankumsatz auf mehrere Posten verteilen.

Ein Prototyp außerhalb dieses Repos hat das Vorgehen mit einem lokalen Modell an realen Avisen
erprobt: Ein mittleres Modell schreibt Nummern zuverlässig ab, beim Rechnen, Summieren und Suchen in
großen Postenlisten ist es unzuverlässig. Fehler beim Abschreiben (mitgezählte Überträge, vergessene
Zeilen, erfundene Beträge) fallen durch eine Summen- und Textprüfung auf.

## Entscheidung
1. **Die KI liest nur.** Je Seite (PDF-Textebene, sonst OCR; lange Seiten geteilt) schreibt die KI die
   Positionen als JSON nach festem Schema ab, Beträge **wörtlich als Text** (`json_schema`, Temperatur 0).
   Sie rechnet nichts um und ordnet nichts zu.
2. **Das Programm prüft die Abschrift.** Jede Nummer und jeder Betrag muss im Seitentext stehen. Sonst
   bekommt die KI die Fehler genannt und einen zweiten Versuch; was dann noch fehlt, macht das Avis rot.
   Zeilen wie „Übertrag“ und „Summe“ ohne Belegnummer entfallen.
3. **Das Programm ordnet zu und erklärt.** Belegnummer → Referenz des offenen Postens (auch ohne führende
   Nullen); ohne Nummer nur über den Betrag auf den Cent beim erkannten Partner, bei unbekanntem Partner
   zusätzlich über Datum oder Endziffern (immer gelb). Skonto aus der Spalte oder als glatter Prozentsatz
   (1, 2, 2,5, 3, 4 %; 3,5 % bewusst nicht, das ist oft Skonto plus Delkredere); Abzüge ohne Posten über
   Stichwörter in mehreren Sprachen. Minuszeilen mit der Nummer einer Rechnung gehören zu dieser.
4. **Ampel.** Grün: vollständig und ohne Annahme erklärt. Gelb: erklärt mit Annahme (Treffer über den
   Betrag, Skonto nach mehr als 30 Tagen, Gutschriften). Rot: Abschrift fehlerhaft, Summe geht nicht auf,
   Posten fehlt oder ist schon ausgeglichen, Abzug ohne Grund, Belegnummer eines offenen Postens steht im
   Avis, aber in keiner Position.
5. **Ausgleich als eine Buchung.** Bank im Soll (überwiesener Betrag), je Posten das Sammelkonto im Haben
   mit Partner (offener Betrag), Skonto auf ein Erlösschmälerungskonto mit Steuercode – die
   Umsatzsteuerkorrektur steht als eigene Zeile –, übrige Abzüge auf ein wählbares Konto. Danach werden
   die Posten mit dieser Buchung ausgeglichen; ein Storno lässt sie wieder aufleben und gibt den
   Bankumsatz frei. Das Avis wird als Beleg verknüpft. Rote Avise, Gutschriften und Fremdwährung
   gleicht das Programm nicht aus.
6. **Im Hintergrund.** Das Auslesen dauert mit lokalen Modellen Minuten; das Avis selbst trägt den Stand
   (`running`, `ready`, `failed`, `applied`). Abschrift und Ergebnis liegen als JSON am Avis, „Neu
   zuordnen“ rechnet ohne KI neu (etwa nach neuen Posten oder Bankumsätzen).
7. **Schnittstellen:** UI (Bank & OPOS → Zahlungsavise), REST `/api/v1/payment-advices`, MCP-Tools
   `start_/list_/get_/rematch_/apply_/delete_payment_advice`; für Obi als Werkzeuggruppe
   „zahlungsavise“ (schreibende nach Bestätigung).

## Konsequenzen
- Positiv: Ein mittleres lokales Modell reicht, weil jeder Fehler der KI als rot auffällt und nicht
  gebucht wird. Alles Rechnen ist nachvollziehbar und testbar.
- Positiv: Ein Avis mit vielen Posten wird mit einem Klick ausgeglichen, inklusive Skonto mit
  Steuerkorrektur.
- Negativ: Ein Skontosatz außerhalb der Liste oder eine Zerlegung wie „2 % Skonto + 0,75 % Delkredere“
  ohne eigene Spalten bleibt rot und muss von Hand geklärt werden.
- Negativ: Übrige Abzüge gehen gesammelt auf ein Konto; wer Bonus, Delkredere und Reklamation getrennt
  braucht, bucht um.
- Offene Punkte: KI-Rückfrage nur für ungeklärte Fälle (Summe, Kategorie, Kandidatenwahl, Zerlegung der
  Differenz), deren Vorschlag das Programm nachrechnet; Skontofrist aus den Zahlungsbedingungen des
  Partners; Gutschriften verrechnen; Avise aus einem Postfach abholen; Excel-/CSV-Avise.

## Alternativen
- **Regeln je Avis-Format:** genau, aber für jeden Kunden und jede Sprache neu zu pflegen.
- **Die KI ordnet selbst zu:** scheitert an großen Postenlisten und am Nachrechnen; Fehler wären nicht
  von richtigen Vorschlägen zu unterscheiden.
- **Bankumsatz je Posten teilen (bestehender Ausgleich):** begrenzt den Ausgleich auf den Bankbetrag und
  lässt Skonto und Abzüge als Rest offen; deshalb eine eigene Buchung mit den Abzügen.
