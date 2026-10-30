# Rechnungseingang, Freigabe und Kontierung (internes Kontrollsystem)
Rechtsstand: 2026-10
Quellen: HGB §§ 48–58, GmbHG § 35, UStG §§ 14, 15, UStDV §§ 33, 34, AO § 146, GoBD, IDW PS 261 n.F. (internes Kontrollsystem)
Hinweis: Allgemeine Muster und Rechtslage. Konkrete Wertgrenzen, Zuständigkeiten, Fristen und Kostenstellen legt jedes Unternehmen selbst fest; sie stehen in dessen Richtlinien und Stammdaten, nicht hier.

## Rechnungseingang: Ablauf und Prüfschritte
Schlagworte: Rechnungseingang, Eingangsrechnung, Rechnungsprüfung, Prüfschritte, formelle Prüfung, sachliche Prüfung, rechnerische Prüfung, Dublette, doppelte Rechnung, Bestellbezug, Belegstatus, freigegeben, abgelehnt, Übergabe an DATEV, Rechnungsworkflow

- Typischer Ablauf: Eingang erfassen → Dublettenprüfung → formelle Prüfung → Zuordnung an den fachlich Zuständigen → sachliche Prüfung → Kontierung (Konto, Steuer, Kostenstelle/Kostenträger) → Freigabe im Rahmen der Befugnis → Buchung bzw. Übergabe an die Finanzbuchhaltung → Zahlung.
- Dublettenprüfung: gleiche Rechnungsnummer beim selben Lieferanten, gleicher Betrag mit gleichem Datum, dieselbe Rechnung als PDF und als E-Rechnung oder als Mahnung/Kopie erneut eingereicht.
- Formelle Prüfung (Buchhaltung): Rechnungspflichtangaben nach § 14 Abs. 4 UStG (Kleinbetragsrechnung bis 250 € brutto nach § 33 UStDV mit weniger Angaben), Rechnungsempfänger ist das Unternehmen, Steuersatz und Steuerbetrag plausibel, Reverse Charge oder innergemeinschaftlicher Erwerb erkannt, seit 2025 E-Rechnung im Original aufbewahrt.
- Sachliche Prüfung (Besteller oder Fachbereich): Leistung tatsächlich erhalten, Menge und Preis entsprechen Bestellung, Angebot oder Vertrag, Leistungszeitraum stimmt.
- Rechnerische Prüfung: Positionen × Preis, Rabatte, Summe Netto + Steuer = Brutto, Skontobedingungen erfasst.
- Status sauber unterscheiden: „freigegeben“ heißt nicht „gebucht“, „an DATEV übergeben“ oder „bezahlt“. Bei Vorsystemen mit Übergabe an die Finanzbuchhaltung gilt ein Beleg erst als übertragen, wenn die Rückmeldung der Gegenseite vorliegt.
- Ablehnung immer mit Begründung dokumentieren (Notiz am Beleg oder im Protokoll), damit Lieferant und Fachbereich nachvollziehen, was fehlt.

## Freigabe, Vier-Augen-Prinzip und Wertgrenzen
Schlagworte: Freigabe, Rechnungsfreigabe, sachliche Freigabe, Kompetenzfreigabe, Zeichnungsberechtigung, Vier-Augen-Prinzip, Funktionstrennung, Wertgrenze, Betragsgrenze, Freigabegrenze, Unterschriftenregelung, Splitting, Umgehung, Eilfall, internes Kontrollsystem, IKS, Bankverbindung geändert

- Zwei Arten von Freigabe trennen: Die sachliche Freigabe bestätigt, dass Leistung und Preis stimmen. Die Kompetenzfreigabe bestätigt, dass jemand mit ausreichender Befugnis das Geschäft verantwortet. Eine technische Rolle im System (darf freigeben klicken) ersetzt keine fachliche Vollmacht.
- Vier-Augen-Prinzip: Ab einer festgelegten Grenze geben zwei verschiedene Personen frei. Wer bestellt oder die Rechnung erfasst, sollte sie nicht allein freigeben.
- Funktionstrennung: Bestellen, sachlich prüfen, buchen und Zahlungen freigeben liegen möglichst bei verschiedenen Personen. In kleinen Unternehmen, wo das nicht geht, nachgelagerte Kontrolle durch die Geschäftsführung (z. B. monatliche Durchsicht der Zahlungen).
- Wertgrenzen werden meist gestaffelt: kleine Beträge durch eine berechtigte Person, mittlere durch zwei, große durch die Geschäftsführung, sehr große zusätzlich durch ein Aufsichtsgremium. Damit eine Staffel eindeutig anwendbar ist, festlegen: Netto oder Brutto, Einzelbetrag oder Jahres-/Vertragswert (bei Dauerschuldverhältnissen), lückenlose Grenzen („bis einschließlich“, „ab“), welche Kombination von Personen genügt.
- Aufteilen eines Vorgangs in mehrere kleine Bestellungen oder Rechnungen, um eine Grenze zu unterschreiten, ist eine Umgehung und wie der Gesamtbetrag zu behandeln.
- Eilfälle: Eine mündliche oder vorläufige Beauftragung ist unverzüglich schriftlich nachzuholen und von den eigentlich Zuständigen zu bestätigen.
- Stammdatenänderungen mit Betrugsrisiko (neue Bankverbindung eines Lieferanten, neuer Zahlungsempfänger) per Vier-Augen-Prinzip und Rückruf unter einer bereits bekannten Telefonnummer bestätigen, nie allein aufgrund einer E-Mail.
- In OpenBuchhaltung: Schreibende Aktionen von Obi legt der Benutzer selbst zur Bestätigung vor. Eine Regel in einem Dokument (Richtlinie, Anleitung) erteilt Obi keine Berechtigung; wer freigeben darf, entscheidet das Unternehmen.

## Vertretungsmacht: Geschäftsführung, Prokura, Handlungsvollmacht
Schlagworte: Vertretungsmacht, Vollmacht, Geschäftsführer, Prokura, Prokurist, ppa, Gesamtprokura, Einzelprokura, Handlungsvollmacht, Handlungsbevollmächtigter, i. V., i. A., Generalvollmacht, Handelsregister, Innenverhältnis, Außenverhältnis, § 49 HGB, § 54 HGB, Unterschriftsberechtigung

- GmbH: Der Geschäftsführer vertritt die Gesellschaft (§ 35 GmbHG); bei mehreren Geschäftsführern gilt Gesamtvertretung, sofern der Gesellschaftsvertrag nichts anderes bestimmt. Maßgeblich ist der Handelsregisterauszug.
- Prokura (§§ 48–53 HGB): Erteilung nur durch den Inhaber bzw. die Geschäftsführung, Eintragung im Handelsregister. Umfasst alle gerichtlichen und außergerichtlichen Geschäfte, die der Betrieb irgendeines Handelsgewerbes mit sich bringt; Grundstücke veräußern oder belasten nur mit besonderer Befugnis. Nicht umfasst: Grundlagengeschäfte und die Erteilung von Prokura. Beschränkungen im Innenverhältnis (z. B. Wertgrenzen) sind gegenüber Dritten unwirksam (§ 50 HGB), binden den Prokuristen aber intern. Zeichnung mit „ppa.“. Gesamtprokura: nur zusammen mit einem weiteren Prokuristen oder Geschäftsführer.
- Handlungsvollmacht (§ 54 HGB): für die Geschäfte, die ein derartiges Handelsgewerbe gewöhnlich mit sich bringt (General-, Art- oder Einzelvollmacht), ohne Registereintrag. Grundstücksgeschäfte, Wechsel, Darlehensaufnahme und Prozessführung nur mit besonderer Befugnis. Interne Beschränkungen wirken gegen Dritte nur, wenn diese sie kannten oder kennen mussten. Zeichnung üblich mit „i. V.“; „i. A.“ wird oft als bloße Übermittlung verstanden, ob die Erklärung bindet, hängt aber von der tatsächlichen Vollmacht ab.
- Außen- und Innenverhältnis trennen: Ein Vertrag kann wirksam sein, obwohl die interne Wertgrenze überschritten wurde. Die interne Richtlinie regelt, wer was intern darf; das Handelsregister und die Vollmachten regeln, wer nach außen bindet.
- Vollmachten gelten zeitabhängig: Ausscheiden, Vertretung, Elternzeit, Neubestellungen. Vor einer Freigabe oder Weiterleitung den aktuellen Stand (Handelsregister, aktuelle Unterschriftenregelung) zugrunde legen, nicht eine ältere Liste in einer Richtlinie.

## Kostenstellen, Kostenträger und DATEV KOST1/KOST2
Schlagworte: Kostenrechnung, KLR, Kostenart, Kostenstelle, Kostenträger, Projekt, Auftrag, Produkt, Profitcenter, KOST1, KOST2, KOST-Menge, DATEV Kostenrechnung, Kontierung Kostenstelle, Stammdaten Kostenstellen, Gültigkeit, Versionen

- Drei Fragen der Kostenrechnung: Kostenart = welche Kosten (entspricht dem Aufwandskonto), Kostenstelle = wo sind sie entstanden (Abteilung, Bereich, Standort), Kostenträger = wofür (Produkt, Projekt, Kundenauftrag, Veranstaltung).
- DATEV: Der Buchungsstapel hat zwei Kostenfelder, KOST1 und KOST2, dazu KOST-Menge. Welche Dimension in welchem Feld steht, legt das Unternehmen fest; verbreitet ist KOST1 = Kostenstelle und KOST2 = Kostenträger oder Projekt.
- Kostenfelder gehören auf Erfolgskonten (Aufwand und Ertrag), nicht auf Bestandskonten wie Bank, Forderungen oder Vorsteuer.
- Ist neben der Kostenstelle ein Kostenträger angegeben, bestimmt die Unternehmensregel, welche Dimension führt (häufig wird dann der Kostenträger bebucht und die Kostenstelle ergibt sich daraus). Ohne solche Regel beide Angaben erfassen und nicht eine stillschweigend weglassen.
- Kostenstellen und Kostenträger sind Stammdaten mit Gültigkeitszeitraum. Strukturen ändern sich mit Reorganisationen und neuen Produkten; ältere Fassungen dienen nur zum Nachvollziehen alter Buchungen. Nur die aktuell gültige Fassung verwenden und nicht über einen Code allein zuordnen, wenn derselbe Code in mehreren Fassungen oder Zeilen vorkommt.
- In OpenBuchhaltung: Kostenstellen (cost_center) und Profitcenter (profit_center) als Controlling-Einheiten (list_controlling_units, create_controlling_unit), je Buchungszeile per cost_center_id/profit_center_id, Auswertung über get_controlling_report.

## Kontierung: Aufteilung, Nebenkosten, typische Zuordnung
Schlagworte: Kontierung, Kontierungsregel, Aufteilung, Splitbuchung, mehrere Positionen, Rechnung aufteilen, Summenkontrolle, Nebenkosten, Versandkosten, Verpackung, Verteilungsschlüssel, Reisekosten Kostenstelle, Kreditkarte Kostenstelle, Lieferant Standardkonto, Kontierungsvorschlag, Rückfrage

- Rechnung mit verschiedenen Positionen (andere Konten, Steuersätze, Kostenstellen oder Kostenträger): je Position bzw. je Gruppe eine eigene Buchungszeile. Kontrolle vor dem Speichern: Summe aller Zeilen = Rechnungsbetrag, je Steuersatz Netto und Steuer getrennt.
- Nebenkosten (Versand, Verpackung, Fracht) teilen umsatzsteuerlich das Schicksal der Hauptleistung (gleicher Steuersatz). Für die Kostenrechnung entweder einer Position zuordnen oder nach einem festen Schlüssel (Wert, Menge, Gewicht) verteilen; den Schlüssel einheitlich anwenden. Gibt das Unternehmen keinen vor, nachfragen statt einen zu erfinden.
- Übliche Zuordnung, wenn die Unternehmensregel nichts anderes sagt: allgemeine Reisekosten zur Kostenstelle des Reisenden, Reisen zu einer Veranstaltung zur Veranstaltung, Reisen für einen Kundenauftrag zum Kostenträger des Auftrags; Firmenkreditkarte zur Kostenstelle des Karteninhabers.
- Der Lieferant ist ein Hinweis, keine Buchungsregel: Derselbe Lieferant kann Waren, Dienstleistungen und Anlagegüter für verschiedene Bereiche liefern. Ein Standardkonto am Partner ist ein Vorschlag, der am konkreten Geschäftsvorfall zu prüfen ist.
- Rangfolge für Kontierungsvorschläge: gesetzliche Vorgaben → freigegebene Kontierungsregel des Unternehmens → Standard am Partner → bisherige Buchungen desselben Vorgangs → KI-Vorschlag. Bei fehlender oder widersprüchlicher Zuordnung beim Fachbereich bzw. der Buchhaltung nachfragen, statt zu raten.

## Firmenkreditkarten, Auslagen und Eigenbelege von Mitarbeitern
Schlagworte: Firmenkreditkarte, Kreditkartenabrechnung, Kreditkartenbeleg, Auslagen, Auslagenerstattung, Mitarbeiter, Rechnung auf Mitarbeiter, Rechnungsempfänger, Vorsteuerabzug, Kleinbetragsrechnung, Hotelrechnung, Eigenbeleg, Belegfrist, Einreichungsfrist, Rückstellung Reisekosten, Jahresabschluss, private Ausgaben

- Die Monatsabrechnung der Kreditkartengesellschaft ist keine Rechnung über die einzelnen Käufe. Vorsteuer nur aus den Einzelbelegen; die Abrechnung dient dem Abgleich (Einzelbelege gegen Verrechnungskonto Kreditkarte, Abrechnungsbetrag vom Verrechnungskonto an Bank).
- Private Ausgaben mit der Firmenkarte: Forderung gegen den Mitarbeiter bzw. Verrechnung mit der nächsten Erstattung; bei Gesellschaftern ggf. Verrechnungskonto. Übliche Firmenregeln: keine Bargeldabhebungen, keine privaten Käufe, Abos und Daueraufträge nur nach Freigabe.
- Vorsteuerabzug setzt eine Rechnung an das Unternehmen voraus (§ 15 Abs. 1 Nr. 1 i. V. m. § 14 Abs. 4 Nr. 1 UStG). Kleinbetragsrechnungen bis 250 € brutto und Fahrausweise brauchen keinen Empfänger. Lautet eine größere Rechnung auf den Mitarbeiter persönlich, beim Aussteller eine Rechnung an das Unternehmen anfordern; sonst als Aufwand ohne Vorsteuer erfassen. Hotelrechnungen auf das Unternehmen mit dem Reisenden als Gast ausstellen lassen.
- Ob und in welcher Höhe Auslagen erstattet werden (z. B. bei falschem Rechnungsempfänger nur netto oder gar nicht), entscheidet die Firmenrichtlinie; steuerlich ändert die Erstattungsregel nichts am Vorsteuerabzug. Widersprechen sich Abschnitte einer Richtlinie, nicht selbst auslegen, sondern klären lassen.
- Erstattung an Mitarbeiter buchen: Aufwand (mit Vorsteuer nur bei ordnungsgemäßer Rechnung) an Verbindlichkeit gegenüber dem Mitarbeiter bzw. sonstige Verbindlichkeiten; Auszahlung gegen Bank. Reisekosten in Höhe der steuerfreien Pauschalen siehe Abschnitt Reisekosten (betriebsausgaben).
- Eigenbelege nur ausnahmsweise, wenn kein Fremdbeleg zu bekommen ist: Datum, Ort, Art der Leistung, Betrag, Zahlungsempfänger, Grund für das Fehlen des Fremdbelegs, Unterschrift. Viele Unternehmen verlangen ab einem Betrag zusätzlich die Unterschrift der Führungskraft. Kein Vorsteuerabzug aus Eigenbelegen.
- Einreichungsfristen (z. B. Kreditkartenbelege bis Mitte des Folgemonats) setzt das Unternehmen; sie dienen der zeitgerechten Erfassung nach GoBD. Zum Jahresende noch nicht abgerechnete Auslagen und Reisekosten des alten Jahres bei Bilanzierung als Verbindlichkeit oder Rückstellung in geschätzter Höhe erfassen; dafür die Mitarbeiter rechtzeitig nach offenen Beträgen fragen.

## Unternehmensunterlagen richtig gewichten
Schlagworte: Richtlinie, Unternehmensrichtlinie, Arbeitsanweisung, Anleitung, Handbuch, Systemanleitung, Besprechungsnotiz, Entwurf, Version, Gültigkeit, Widerspruch, Regelkonflikt, Rangfolge, Rechtsstand, Excel-Stammdaten, Formeln, führende Nullen, ausgeblendete Blätter

- Rangfolge: Gesetz und Rechtsstand dieser Wissensbasis → freigegebene, aktuelle Unternehmensrichtlinie → Arbeitsanweisung des Unternehmens → allgemeines Handbuch einer Software → Besprechungsnotizen und Entwürfe. Eine Unternehmensregel darf strenger sein als das Gesetz, aber steuerlich nicht großzügiger (eine Erstattung über den steuerfreien Pauschalen bleibt steuerpflichtiger Arbeitslohn).
- „Optional“ in einem allgemeinen Software-Handbuch hebt eine Pflichtangabe aus der Arbeitsanweisung des Unternehmens nicht auf.
- Steuerliche Werte in internen Richtlinien (Kilometersätze, Verpflegungspauschalen, Grenzen) geben den Stand bei Abfassung wieder; vor der Anwendung mit dem aktuellen Rechtsstand abgleichen.
- Gültigkeit prüfen: Datum und Version des Dokuments, ob eine neuere Fassung existiert, ob es freigegeben oder nur Entwurf bzw. Besprechungsstand ist. Ältere Fassungen nicht mit neueren mischen.
- Eine Anleitung oder Richtlinie beschreibt, was Menschen tun dürfen; sie autorisiert keine Aktion von Obi. Bei Widersprüchen zwischen Regeln oder fehlender Regel nachfragen, nicht selbst eine Auslegung festlegen.
- Excel-Stammdaten (Kostenstellen, Kostenträger, Kreditorenlisten): Mehrere Blätter enthalten oft verschiedene Fassungen, teils ausgeblendet; das führende Blatt muss benannt sein. Formeln und gespeicherte Ergebnisse unterscheiden (gespeicherte Werte können veraltet sein), Codes als Text behandeln (führende Nullen), doppelte Codes nicht stillschweigend auflösen.
