# ADR-008: Konsolidierte Auswertung mehrerer Gesellschaften

- **Status:** Angenommen
- **Datum:** 2026-11-03
- **Entscheider:** OpenBuchhaltung Kernteam
- **Umsetzung:** `app/services/consolidation.py`, `app/api/consolidation.py`,
  `app/web/consolidation.py`; `docs/umsetzungsplan.md`, V2

## Kontext
Gruppen mit mehreren Gesellschaften im selben Mandanten wollen ihre Zahlen zusammen sehen:
Was steht in der Summe da, wenn Forderungen und Umsätze zwischen den eigenen Gesellschaften
herausgerechnet sind? Bisher gibt es nur Einzelberichte je Gesellschaft.

Ein handelsrechtlicher Konzernabschluss (§§ 290 ff. HGB) ist etwas anderes und viel größer:
Kapitalkonsolidierung nach Regeln, Zwischenergebnisse, latente Steuern, Equity-Methode,
Währungsumrechnung, Anhang, Prüfung. Das ist hier nicht das Ziel.

## Entscheidung

### 1. Auswertung statt Bücher
- Die Auswertung rechnet ausschließlich aus den Büchern der Mitglieder. Automatische
  Eliminierungen werden bei jedem Bericht berechnet und nicht gespeichert. Konsolidierungsbuchungen
  stehen nie in den Büchern einer Gesellschaft; Festschreibung, Hash und Trigger bleiben unberührt.
- `consolidation_group` (Mandant, Name, Muttergesellschaft, aktiv) und `consolidation_member`
  (Gesellschaft, Beteiligungsquote, einbezogen ab/bis). Alle Mitglieder gehören zum selben Mandanten
  und führen dieselbe Währung; das wird beim Speichern und beim Bericht geprüft. Die
  Muttergesellschaft ist immer Mitglied und wird mit 100 % geführt. Eine Gesellschaft darf in
  mehreren Gruppen stehen (Teilkonzern und Gesamtgruppe).

### 2. Zusammenfassung über die Kontonummer
- Eine Zeile je Kontonummer mit Spalten je Gesellschaft, Summe, Eliminierung, manueller Buchung und
  konsolidiertem Wert. Bezeichnung und Kontoart kommen von der ersten Gesellschaft, die das Konto
  führt (Muttergesellschaft zuerst).
- Unterschiedliche Kontenrahmen und gleiche Nummern mit anderer Kontoart sind kein Fehler, sondern
  Warnungen im Bericht: Die Zusammenfassung trifft dann fachlich nicht, die Bilanz geht aber auf.
  `income` und `revenue` gelten als dieselbe Kontoart.
- Jede Gesellschaft folgt denselben Regeln wie ihre Einzelberichte; `reports.py` stellt dafür die
  Buchungsmengen bereit (`trial_balance_conditions`, `income_statement_conditions`,
  `balance_sheet_conditions`), statt sie zu kopieren.

### 3. Verbundene Partner kennzeichnen
- `business_partner.intercompany_company_id` zeigt auf die Gesellschaft, für die der Partner steht:
  gleicher Mandant, nicht die eigene Gesellschaft, gepflegt über die bestehende Partner-Änderung mit
  Historie. Das hängt am Partner und nicht an der Buchung, damit bestehende und festgeschriebene
  Buchungen ohne Änderung ausgewertet werden können. Kein Hashwechsel.

### 4. Automatische Eliminierungen
- **Schuldenkonsolidierung:** Salden (Soll − Haben) auf Sammelkonten (`account.subledger`) mit einem
  Partner, der auf ein Mitglied zeigt, werden je Paar aufgerechnet. Andere Konten (z. B. Darlehen
  ohne Sammelkonto-Kennzeichen) bleiben stehen und sind manuell zu konsolidieren.
- **Aufwands- und Ertragskonsolidierung:** Erfolgszeilen von Buchungen, in denen ein solcher Partner
  vorkommt. Der eigene Partner der Zeile entscheidet; ohne ihn gilt der Partner der Buchung, wenn er
  eindeutig ist. Bei mehreren oder gemischten Partnern lässt sich der Anteil nicht bestimmen: Die
  Zeile bleibt stehen, und der Bericht warnt mit der Buchungsnummer. Lieber sichtbar stehen lassen
  als falsch aufteilen.
- **Differenzen:** Sieht A 100 und B 90, werden beide Salden herausgenommen; die 10 stehen je Paar in
  der Eliminierungsliste, als Warnung und auf einer eigenen Zeile „Aufrechnungsdifferenz“: bei den
  Schulden in der Bilanz (Soll-Saldo aktiv, Haben-Saldo passiv), bei Aufwand und Ertrag in der GuV.
  Damit bleibt das konsolidierte Ergebnis die Summe der Gesellschaften, und die Bilanz geht auf.
- Steuerkonten werden nicht eliminiert: Umsatzsteuer und Vorsteuer bestehen gegenüber dem Finanzamt.
  Organschaften bildet die Auswertung nicht ab.

### 5. Einbezogen ab/bis
- SuSa und GuV zählen nur Buchungen im Schnitt aus Zeitraum und Einbeziehung; eliminiert wird, wenn
  auch die Gegenseite am Buchungstag einbezogen ist.
- Die Bilanz ist eine Stichtagsaufnahme: Ist die Gesellschaft zum Stichtag einbezogen, steht ihr
  voller Bestand im Bericht, einschließlich des Ergebnisses vor der Einbeziehung. Sonst fehlt sie
  ganz, und Salden gegen sie bleiben als fremde Salden stehen (mit Warnung). Das Jahresergebnis der
  Bilanz kann dadurch von der GuV abweichen; der Bericht weist darauf hin.

### 6. Manuelle Konsolidierungsbuchungen: löschen statt stornieren
- `consolidation_adjustment` + Zeilen: Gruppe, Datum, Art (`capital`, `other`), Text, Zeilen auf
  Kontonummern, die es in mindestens einem Mitglied gibt; muss ausgeglichen sein. Bezeichnung und
  Kontoart werden festgehalten, damit die Zeile einzuordnen bleibt, wenn das Mitglied die Gruppe
  verlässt.
- **Änderungen nur durch Löschen und neu Erfassen, mit Audit-Eintrag.** Die Buchungen sind Teil einer
  Auswertung und keine Handelsbücher, sie werden nicht festgeschrieben; ein Storno brächte nur
  Gegenzeilen in den Bericht, ohne dass etwas nachvollziehbarer würde. Der vollständige Inhalt und die
  optionale Begründung stehen als Vorher-Wert im verketteten Audit-Log (`action=deleted`).
- Wirkung im Bericht: Bilanz bis zum Stichtag, GuV im Zeitraum, SuSa im Zeitraum. Ältere Buchungen
  wirken wie ein Saldovortrag weiter: Bestandskonten behalten sie, Erfolgskonten laufen in die Zeile
  „Ergebnisvortrag aus Konsolidierungsbuchungen“. Grenze ist in der Bilanz der Beginn des Geschäftsjahres
  der Muttergesellschaft zum Stichtag, in der SuSa das Von-Datum. So wirkt eine Kapitalkonsolidierung
  dauerhaft, und das Jahresergebnis enthält nur das laufende Jahr.

### 7. Beteiligungsquote nur als Information
Unter 100 % zeigt der Bericht die Quote, den rechnerischen Fremdanteil und einen Hinweis. Es gibt
keine automatische Berechnung von Minderheitenanteilen; die Buchung erfolgt manuell.

### 8. Prüferexport über die Muttergesellschaft
Der Export bleibt je Gesellschaft. Gruppen, Mitglieder, Buchungen und `consolidation_history` gehören
zum Paket der Muttergesellschaft; die Audit-Einträge tragen ihre `company_id`. Der Export einer
Tochter enthält die Gruppe nicht, aber ihren Partner mit `intercompany_company_id`.

### 9. Schnittstellen
UI `/konsolidierung`, REST unter `/api/v1/consolidation-groups…`, CSV unter
`/api/v1/exports/consolidation-report.csv`, MCP-Tools `*_consolidation_*`. Gruppen sind wie Gesellschaften
mandantengetrennt, der globale Admin sieht alle. Im KI-Chat läuft der Bericht sofort, Änderungen nach
Bestätigung; nichts davon ist gesperrt, weil keine Bankdaten oder Zahlungen betroffen sind.

## Konsequenzen
- Positiv: Keine neue Buchführung und kein Eingriff in den Compliance-Kern; der Bericht ist jederzeit
  aus den Einzelbüchern reproduzierbar; Abstimmungsdifferenzen zwischen den Gesellschaften werden
  sichtbar.
- Negativ: Die Eliminierung ist nur so gut wie die Kennzeichnung der Partner; Buchungen ohne Partner
  bleiben stehen. Die Zusammenfassung über die Kontonummer setzt praktisch einen gemeinsamen
  Kontenrahmen voraus. Der Bericht wird bei jedem Aufruf neu gerechnet.
- Offene Punkte: Automatische Kapitalkonsolidierung, Vorschläge für nicht gekennzeichnete Partner.

**Grenzen, bewusst nicht enthalten:** keine Zwischenergebniseliminierung, keine latenten Steuern, keine
Equity-Methode, keine Fremdwährungsumrechnung von Töchtern, kein Konzernanhang, nicht geprüft. Kein
Konzernabschluss nach §§ 290 ff. HGB und kein Ersatz für einen solchen.

## Alternativen
- **Konsolidierungsbuchungen als Journalbuchungen einer eigenen „Konsolidierungsgesellschaft“:**
  verworfen; das vermischt Auswertung und Bücher, zieht UStVA, DATEV-Export und Festschreibung mit
  hinein und verlangt Kontenpläne für eine Gesellschaft, die es nicht gibt.
- **Eliminierungen speichern:** verworfen; sie veralten mit jeder Buchung und brauchen eine eigene
  Abstimmung. Berechnete Werte lassen sich nicht vom Buchungsstand entfernen.
- **Abbildung auf einen Gruppen-Kontenrahmen:** verworfen für diesen Schritt; lohnt erst bei
  unterschiedlichen Kontenrahmen und braucht eine eigene Pflegeoberfläche. Die Warnungen machen den
  Bedarf sichtbar.
- **Storno statt Löschen bei manuellen Buchungen:** siehe Entscheidung 6.
- **Differenzen stillschweigend in die Eigenkapitalzeile:** verworfen; die Differenz soll auffallen,
  damit sie in den Büchern geklärt wird.
