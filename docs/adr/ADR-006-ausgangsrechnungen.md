# ADR-006: Firmenstammdaten und Ausgangsrechnungen

- **Status:** Angenommen
- **Datum:** 2026-11-03
- **Entscheider:** OpenBuchhaltung Kernteam
- **Umsetzung:** `app/services/sales_invoices.py`, `sales_invoice_pdf.py`,
  `company_master_data.py`, `einvoice_export.py`; Migration `20261103_0063`

## Kontext
Bisher ließ sich eine Ausgangsrechnung nur als XML aus einem Formular erzeugen: nicht gespeichert,
ohne Nummernkreis, Buchung, offenen Posten und PDF. Die eigenen Firmendaten kamen aus den
Umgebungsvariablen `SELLER_*`, sodass mehrere Gesellschaften einer Instanz nicht getrennte Angaben
führen konnten (Projektreview 2026-09-19, Punkte #2, #3, #7).

## Entscheidung

### 1. Firmenstammdaten an der Gesellschaft
`company` erhält Anschrift, USt-IdNr, Steuernummer, Kontakt, Handelsregister, Geschäftsführung, Bank,
`invoice_prefix` und `is_small_business`. Änderungen stehen mit Vorher/Nachher im Audit-Log. Die IBAN
bleibt beim gesperrten Weg `set_company_bank_details`. `SELLER_*` bleibt Rückfallwert für leere Felder,
damit bestehende Installationen ohne Pflege weiterlaufen.

### 2. Rechnung als eigene Entität mit Lebenszyklus
`sales_invoice` und `sales_invoice_line` mit den Zuständen `draft`, `issued`, `cancelled`:

- **Entwurf:** frei änderbar und löschbar, keine Nummer, keine Buchung.
- **Ausstellen ist atomar:** Pflichtangaben prüfen, Nummer ziehen, buchen, offenen Posten anlegen, XML
  und PDF erzeugen und als Belege ablegen. Scheitert ein Schritt, fällt alles zurück, auch geschriebene
  Dateien. Dafür haben `create_open_item` und `reverse_journal_entry` den Parameter `commit=False`
  bekommen.
- **Unveränderlich:** Service und Datenbank-Trigger lassen an einer ausgestellten Rechnung nur noch den
  Wechsel `issued` → `cancelled` zu. Die PDF- und XML-Belege lassen sich weder ersetzen noch umhängen.
  Die Buchung lässt sich nicht direkt stornieren und Leistungsdatum und Partner nicht nachträglich
  ändern; die Kontierung (Kostenstelle) darf ergänzt werden.

### 3. Lückenloser Nummernkreis
`sales_invoice_sequence` je Gesellschaft und Jahr des Rechnungsdatums, Format
`{prefix}-{jahr}-{nummer:04d}`. Die Nummer wird in der Transaktion des Ausstellens mit einem `UPDATE` auf
die Zählerzeile gezogen. Das sperrt die Zeile bis zum Commit (auch unter SQLite ist es die Schreibsperre)
und ein Rollback nimmt die Nummer zurück.

### 4. Steuer je Satz gerundet
Die Steuer wird je Steuersatz aus der Summe gerundet (EN 16931, `compute_tax_groups`), nicht je Position.
Damit die Buchung der Rechnung entspricht, erzeugt das Ausstellen die Steuerzeilen selbst
(`expand_tax_lines=False`) statt über die automatische Teilbuchung, die je Zeile rundet. Teilen sich
mehrere Steuercodes einen Satz, trägt der mit der größten Grundlage die Rundungsdifferenz.

### 5. Steuerbehandlung am Rechnungskopf
`tax_treatment` bestimmt Steuerkategorie, Befreiungsgrund und Pflichthinweis:

| Wert | Kategorie | VATEX | Prüfung beim Ausstellen |
|---|---|---|---|
| `standard` | S | | Steuercode mit Satz > 0 je Position |
| `reverse_charge` | AE | VATEX-EU-AE | USt-IdNr des Kunden |
| `intra_community` | K | VATEX-EU-IC | USt-IdNr beider Seiten, Kunde in anderem EU-Staat |
| `export` | G | VATEX-EU-G | Kunde außerhalb der EU |
| `exempt` | E | | |
| `small_business` | E | | Kennzeichen in den Firmendaten; dort Pflicht |

Außer bei `standard` ist ein Steuercode mit Satz > 0 ein Fehler, denn ausgewiesene Steuer würde nach
§ 14c UStG geschuldet. Passt das Erlöskonto nicht zur Kennzahl der UStVA, erscheint ein Hinweis, das
Ausstellen bleibt möglich. Der Nullsatz nach § 12 Abs. 3 UStG ist nicht eigens abgebildet; er läuft über
`exempt` und den Fußtext. Die konkrete Befreiungsnorm bei `exempt` gehört ebenfalls in den Fußtext.

### 6. Storno per Stornorechnung
Nur solange der Posten unbezahlt ist (`open_amount == original_amount`); nach einer Teilzahlung wird der
Storno abgewiesen, die Zahlung muss zuerst zurückgenommen werden. Die Stornorechnung hat eine eigene
Nummer, negative Mengen und Beträge in Datenbank, API und PDF, verweist auf das Original und bucht über
`reverse_journal_entry`. Das XML ist eine Gutschrift (Typ 381, in UBL Wurzel `CreditNote`) mit positiven
Beträgen, wie EN 16931 es vorsieht. Dafür liest der Import nun auch `CreditNote` und erkennt den Typ;
`book_einvoice` weist Gutschriften ab, statt sie als Rechnung mit positivem Betrag zu buchen. Das gilt
auch für CII mit Typ 381, das bisher unbemerkt als Rechnung gebucht wurde.

### 7. Ausgabe
- **Maßgeblich ist das XML:** XRechnung 3.0 in UBL (Standard) oder CII. `build_cii` enthält nun auch die
  Positionen und die Anschriften, die dort bisher fehlten; ohne sie war das XML nicht EN-16931-konform.
- **PDF mit `reportlab`,** das XML ist als Dateianhang eingebettet (`pypdf`). Das ist **kein** zertifiziertes
  ZUGFeRD/Factur-X-PDF/A-3. Die Standardschrift Helvetica kennt nur Windows-1252; andere Zeichen erscheinen
  im PDF in ihrer Grundform (Ł → L), im XML unverändert.
- **Pflichtfelder der XRechnung:** Fehlen Telefon, E-Mail oder IBAN, verhindert das die Rechnung nach § 14 UStG
  nicht und das Ausstellen bleibt möglich; ein Hinweis nennt die Lücke. Ohne Käuferreferenz trägt BT-10 die
  Debitorennummer, öffentliche Auftraggeber verlangen die Leitweg-ID.
- **Nicht geprüft** gegen den KoSIT-Validator und die XSDs: beides lag nicht offline vor. Geprüft sind der
  Rundlauf durch den eigenen Import und die Elementreihenfolge in Tests.

### 8. Bewusst nicht enthalten
E-Mail-Versand (es gibt noch keinen Mailer), Fremdwährung (nur Firmenwährung), Abschlagsrechnungen,
Skonto-Automatik, wiederkehrende Rechnungen, Gutschriften auf eine Teilmenge, Hinweis auf die
Aufbewahrungspflicht bei Grundstücksleistungen an Privatpersonen (§ 14 Abs. 4 Nr. 9 UStG, derzeit über den
Fußtext).

## Konsequenzen
- Positiv: Rechnung, Buchung, offener Posten und Beleg entstehen aus einem Vorgang und stimmen überein.
- Positiv: Bestehende Exporte und das Formular auf der Seite E-Rechnung laufen weiter; dieses erzeugt nur XML
  ohne Nummernkreis.
- Negativ: `reportlab` samt `pillow` und `chardet` sind neue Abhängigkeiten.
- Offene Punkte: Für die Fremdwährung sind `invoice_currency` und `ledger_amount` die Stellen, an denen die
  Rechnungswährung geprüft bzw. in die Buchungswährung umgerechnet wird; das XML und PDF führen
  `currency_code` bereits durch.

## Alternativen
- **Festschreiben beim Ausstellen:** Die Buchung bliebe sofort unveränderlich, die Kontierung ließe sich aber
  nicht mehr ergänzen. Stattdessen sperrt `amend_journal_entry` nur Leistungsdatum und Partner.
- **Nummer im Entwurf vergeben:** ergäbe Lücken, sobald ein Entwurf gelöscht wird.
- **Negative Beträge im XML mit Typ 380:** Empfängersysteme erkennen eine Gutschrift am Typ; BR-27 verbietet
  negative Einzelpreise, deshalb sind auch Nachlässe negative Mengen.
- **Zertifiziertes PDF/A-3:** braucht Farbprofil, eingebettete Schriften und XMP-Metadaten. Für die
  E-Rechnungspflicht reicht das XML, deshalb später.

## Nachtrag: Storno und Istversteuerung
Der Storno einer Rechnung schließt ihren Posten über `close_open_item_by_cancellation`, nicht über einen
Ausgleich. Es floss kein Geld, also entsteht keine Zeile in `open_item_settlement`; bei Istversteuerung
(ADR-009) gälte sie sonst als Zahlung und löste die volle Umsatzsteuer aus. Ab 2028 trägt die Rechnung eines
Istversteuerers den Hinweis „Versteuerung nach vereinnahmten Entgelten“ (§ 14 Abs. 4 Nr. 6a UStG).
