# ADR-012: Lohnsteuer nach amtlichem PAP und Lohnsteuer-Anmeldung

- **Status:** Angenommen
- **Datum:** 2026-11-22
- **Entscheider:** OpenBuchhaltung Kernteam

## Kontext
Der Lohn-MVP rechnete Lohnsteuer, Kirchensteuer und Solidaritätszuschlag über Raten je Mitarbeiter oder über einen
externen Befehl (`PAYROLL_PAP_COMMAND`), den es nicht gab. Eine Lohnsteuer-Anmeldung fehlte.

Das BMF veröffentlicht jedes Jahr den Programmablaufplan (PAP) für die maschinelle Lohnsteuerberechnung, auch als
XML-Pseudocode (ITZBund): Eingaben, Ausgaben, Konstanten und Methoden, die Ausdrücke in Java mit `BigDecimal`. Die
Datei ist ein amtliches Werk (§ 5 UrhG). Für Tests gibt es die Schnittstelle des BMF-Steuerrechners
(`bmf-steuerrechner.de/interface/…`), deren Ergebnisse als Prüfwerte dienen.

Der amtliche Datensatz der Lohnsteuer-Anmeldung (ERiC, Datenart `LStA`) teilt jede Steuerart in Vorjahr, laufendes
Jahr und Folgejahr auf; Kz 48 ist 41 + 42 + 44 − 33 − 45, Kz 83 die Summe aus 47, 48, 49 und den Kirchensteuern.

## Entscheidung
1. **PAP aus dem amtlichen XML erzeugen, nicht von Hand nachbauen.** `tools/pap_codegen.py` übersetzt das XML
   Element für Element in ein Python-Modul (`app/services/pap/lohnsteuer<Jahr>.py`). Die Java-Ausdrücke bleiben
   wörtlich erhalten und laufen gegen `app.services.pap.runtime.BD`, das die benutzten `BigDecimal`-Methoden mit
   Java-Semantik nachbildet (exakte Division ohne Skala, Rundung bei Skala, `valueOf(double)` über die kürzeste
   Dezimaldarstellung). Unbekannte Konstrukte brechen die Erzeugung ab. Das XML liegt unter `data/pap/`; ein Test
   prüft, dass das eingecheckte Modul genau dem Generator-Ergebnis entspricht, und gleicht 207 Prüfwerte der
   BMF-Schnittstelle ab. Ein neues Jahr heißt: XML ablegen, Modul erzeugen, in `PAP_MODULES` eintragen, Prüfwerte
   ergänzen.
2. **Maßgebend ist das Jahr des Lohnmonats** (§ 38a Abs. 1 Satz 2 EStG). Für Jahre ohne PAP rechnet ein Mitarbeiter
   mit Berechnungsart `pap` nicht, statt still auf Raten zurückzufallen.
3. **Berechnungsart je Mitarbeiter.** `tax_calculation = pap` rechnet nach dem integrierten PAP, `manual` nach den
   Raten. Bestehende Mitarbeiter bleiben bei `manual` (Migration `20261122_0070`), neue in der Oberfläche starten mit
   `pap`. Ein konfigurierter `PAYROLL_PAP_COMMAND` hat weiter Vorrang.
4. **PAP-Merkmale im Mitarbeiterstamm:** Kirchensteuermerkmal (ELStAM-Kürzel), KV-Zusatzbeitrag, PKV mit Beitrag
   und Arbeitgeberzuschuss, Kinderzahl für die Pflegeversicherung (Zuschlag für Kinderlose ab 23, Abschläge ab dem
   zweiten Kind), Renten- und Arbeitslosenversicherungspflicht, Frei- und Hinzurechnungsbetrag, Faktor. Sachsen
   (Pflegeversicherung) und der Altersentlastungsbetrag ergeben sich aus Bundesland und Geburtsdatum; beide sind
   für `pap` Pflicht.
5. **Kirchensteuer** = Bemessungsgrundlage `BK` des PAP × 8 % (Bayern, Baden-Württemberg) bzw. 9 %, auf Cent
   abgerundet. Lohnlaufzeilen halten das Kirchensteuermerkmal fest.
6. **Lohnsteuer-Anmeldung aus gebuchten Lohnläufen nach Zahlungsdatum** (§ 38 Abs. 3, § 41a EStG), Einzelaufstellung
   nach dem Jahr des Lohnmonats. Zeiträume Monat, Quartal, Jahr. Festgehalten wird ein unveränderlicher Snapshot
   (`wage_tax_return`); eine Änderung ist eine Berichtigung mit fortlaufender `correction_number` (Kz 10). Kirchensteuer
   ohne bekanntes Merkmal blockiert das Festhalten. Die Anmeldung gleicht den Gesamtbetrag mit dem Haben der
   Lohnbuchungen auf den Lohnsteuer-Verbindlichkeitskonten ab.

## Konsequenzen
- Positiv: Amtlich korrekte Lohnsteuer für laufenden Monatslohn, nachvollziehbar (Eingaben, Ausgaben, PAP-Version
  und Hash des XML in der Lohnlaufzeile). Jahreswechsel ohne Handarbeit am Rechenkern. UI, REST und MCP
  (`calculate_wage_tax`, `update_payroll_employee`, `preview_wage_tax_return`, `create_wage_tax_return`,
  `list_wage_tax_returns`).
- Negativ: Sonstige Bezüge, Versorgungsbezüge, Pauschalsteuer (Kz 41/44/47), BAV-Förderbetrag und Seeschifffahrt
  sind nicht abgebildet, obwohl der PAP sie rechnen kann. Kirchensteuer-Kappung und Mindestkirchensteuer fehlen.
  Die Fälligkeit berücksichtigt Wochenenden, keine Feiertage. Sozialversicherung bleibt bei Raten.
- Offene Punkte:
  - ElsterXML-Datensatz der LStA und Übermittlung über ERiC (setzt ADR-011 und die Hersteller-ID voraus).
  - PAP 2027 einbinden, sobald veröffentlicht; Prüfwerte gegen die BMF-Schnittstelle ergänzen.
  - Weitere Kirchensteuermerkmale (Kz 65–78), sonstige Bezüge, Lohnsteuerbescheinigung (ElsterLohn2), ELStAM.

## Alternativen
- **PAP von Hand in Python übertragen:** fehleranfällig und jedes Jahr erneut. Verworfen.
- **Fremdbibliothek einbinden:** keine gepflegte Python-Umsetzung mit passender Lizenz; die Abhängigkeit hinge am
  Pflegetakt Dritter. Verworfen.
- **Nur externer Runner (`PAYROLL_PAP_COMMAND`):** bleibt möglich, verschiebt aber die Arbeit auf jeden Betreiber.
- **Anmeldung nach Lohnmonat statt Zahlungsdatum:** entspricht nicht § 41a EStG; die amtliche Einzelaufstellung
  nach Jahren setzt die Zuordnung nach Zahlung voraus.
