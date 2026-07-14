# Compliance-Testkatalog

Dieser Testkatalog beschreibt fachliche und technische Tests, die fuer eine GoBD-orientierte und prueferfreundliche OpenBuchhaltung-Version nachweisbar durchgefuehrt werden sollten.

## 1. Testgrundsaetze

Jeder Testfall sollte dokumentieren:

- Test-ID
- Anforderung/Referenz
- Ausgangsdaten
- Schritte
- erwartetes Ergebnis
- tatsaechliches Ergebnis
- automatisiert oder manuell
- verantwortliche Person
- Datum und Softwareversion
- Link zu CI-Lauf, Screenshot oder Exportartefakt

## 2. Kernbuchhaltung

| ID | Testfall | Erwartung | Art | Prioritaet |
|---|---|---|---|---|
| T-BOOK-001 | Einfache Soll/Haben-Buchung erfassen | Buchung wird gespeichert, Journal ist ausgeglichen | automatisiert | hoch |
| T-BOOK-002 | Unaussgeglichene Buchung erfassen | Speicherung wird mit klarer Fehlermeldung abgewiesen | automatisiert | hoch |
| T-BOOK-003 | Buchung mit USt19 | Steuerzeile wird korrekt berechnet und gebucht | automatisiert | hoch |
| T-BOOK-004 | Buchung mit VSt19 | Vorsteuerzeile wird korrekt berechnet und gebucht | automatisiert | hoch |
| T-BOOK-005 | Steuerfreie Buchung | Keine Steuerzeile, korrekte Kennzeichnung fuer Auswertung | automatisiert | mittel |
| T-BOOK-006 | Rundungsfall mit Centbetraegen | Summe bleibt ausgeglichen, Steuerbetrag nachvollziehbar | automatisiert | hoch |
| T-BOOK-007 | Mehrzeilige Splitbuchung | Buchung wird korrekt gespeichert und exportiert | automatisiert | hoch |
| T-BOOK-008 | Buchung ohne Pflichtfelder | Speicherung wird abgewiesen | automatisiert | mittel |

## 3. Festschreibung und Storno

| ID | Testfall | Erwartung | Art | Prioritaet |
|---|---|---|---|---|
| T-FIN-001 | Einzelne Buchung festschreiben | Status, Zeitpunkt und Benutzer werden gesetzt | automatisiert | hoch |
| T-FIN-002 | Doppelte Festschreibung | Aktion wird abgewiesen oder bleibt idempotent nachvollziehbar | automatisiert | hoch |
| T-FIN-003 | Festgeschriebene Buchung per UI aendern | Aenderung wird verhindert | automatisiert | hoch |
| T-FIN-004 | Festgeschriebene Buchung per API aendern | Aenderung wird verhindert | automatisiert | hoch |
| T-FIN-005 | Festgeschriebene Buchung per DB-Update aendern | Datenbank verhindert Aenderung | automatisiert | hoch |
| T-FIN-006 | Storno erzeugen | Gegenbuchung entsteht, Original bleibt unveraendert | automatisiert | hoch |
| T-FIN-007 | Doppelstorno versuchen | Zweites Storno wird verhindert | automatisiert | hoch |
| T-FIN-008 | Storno einer Stornobuchung | Aktion wird verhindert | automatisiert | hoch |
| T-FIN-009 | Storno in gesperrter Periode | Aktion wird verhindert | automatisiert | hoch |
| T-FIN-010 | Festschreibelauf bis Datum | Alle passenden Buchungen werden festgeschrieben, spaetere bleiben offen | automatisiert | hoch |
| T-FIN-011 | Inhaltshash bei Festschreibung | Kopfdaten und Zeilen werden deterministisch per SHA-256 versiegelt | automatisiert | hoch |
| T-FIN-012 | Privilegierte Buchungsmanipulation | Gemeinsame Integritaetspruefung meldet eine Hashabweichung | automatisiert | hoch |
| T-FIN-013 | Migration festgeschriebener Altbestaende | Migration erzeugt gueltige Inhaltshashes und erhaelt DB-Schutz | automatisiert | hoch |

## 4. Perioden und Jahresabschluss

| ID | Testfall | Erwartung | Art | Prioritaet |
|---|---|---|---|---|
| T-PER-001 | Periode sperren | Buchungen in Periode werden fuer Schreibrollen verhindert | automatisiert | hoch |
| T-PER-002 | Periode entsperren als Buchhalter | Aktion wird abgewiesen | automatisiert | hoch |
| T-PER-003 | Periode entsperren als Admin | Aktion wird protokolliert und erlaubt | automatisiert | mittel |
| T-PER-004 | Jahresabschluss durchfuehren | GuV-Konten werden abgeschlossen, Jahr wird gesperrt | automatisiert | hoch |
| T-PER-005 | Nachbuchung in abgeschlossenes Jahr | Aktion wird verhindert | automatisiert | hoch |
| T-PER-006 | Abschluss erneut ausfuehren | Doppelabschluss wird verhindert oder nachvollziehbar idempotent behandelt | automatisiert | hoch |

## 5. Belege und Archiv

| ID | Testfall | Erwartung | Art | Prioritaet |
|---|---|---|---|---|
| T-DOC-001 | PDF hochladen | Datei wird gespeichert, Metadaten und Hash werden erzeugt | automatisiert | hoch |
| T-DOC-002 | JPG/PNG hochladen | Datei wird gespeichert, Metadaten und Hash werden erzeugt | automatisiert | mittel |
| T-DOC-003 | Nicht erlaubter Dateityp | Upload wird abgewiesen | automatisiert | hoch |
| T-DOC-004 | Datei ueber Groessenlimit | Upload wird abgewiesen | automatisiert | mittel |
| T-DOC-005 | Beleg mit Buchung verknuepfen | Link ist in Buchung und Belegindex sichtbar | automatisiert | hoch |
| T-DOC-006 | Zugeordneten Beleg loeschen | Loeschung wird verhindert oder nur als dokumentierter Sonderprozess erlaubt | automatisiert | hoch |
| T-DOC-007 | Beleg ersetzen | Neue Version wird erzeugt, Original bleibt erhalten | automatisiert | hoch |
| T-DOC-008 | Belegmanipulation im Dateisystem | Integritaetspruefung meldet Hashabweichung | manuell/automatisiert | hoch |
| T-DOC-009 | Belegdatum erfassen | Belegdatum und technischer Uploadzeitpunkt werden getrennt gespeichert und exportiert | automatisiert | hoch |
| T-DOC-010 | OCR-/E-Rechnungsdatum übernehmen | Erkanntes oder eingebettetes Rechnungsdatum wird als Belegdatum gespeichert | automatisiert | hoch |

## 6. Audit-Log

| ID | Testfall | Erwartung | Art | Prioritaet |
|---|---|---|---|---|
| T-AUD-001 | Buchung erfassen | Audit-Eintrag mit Benutzer, Mandant, Objekt und Aktion | automatisiert | hoch |
| T-AUD-002 | Buchung festschreiben | Audit-Eintrag wird erzeugt | automatisiert | hoch |
| T-AUD-003 | Storno erzeugen | Audit-Eintrag fuer Original und Storno nachvollziehbar | automatisiert | hoch |
| T-AUD-004 | Benutzer/Rolle aendern | Audit-Eintrag wird erzeugt | automatisiert | hoch |
| T-AUD-005 | Export erzeugen | Export wird mit Parametern protokolliert | automatisiert | mittel |
| T-AUD-006 | Audit-Eintrag aendern oder loeschen | DB verhindert Aenderung und Loeschung | automatisiert | hoch |
| T-AUD-007 | Hashkette pruefen | Unveraenderte Kette ist gueltig | automatisiert | hoch |
| T-AUD-008 | Hashkette manipulieren | Pruefung erkennt Abweichung | automatisiert | hoch |

## 7. Rollen, Mandanten und API/MCP

| ID | Testfall | Erwartung | Art | Prioritaet |
|---|---|---|---|---|
| T-IAM-001 | Pruefer ruft Journal auf | Lesender Zugriff erlaubt | automatisiert | hoch |
| T-IAM-002 | Pruefer erzeugt Buchung | Schreibzugriff wird verweigert | automatisiert | hoch |
| T-IAM-003 | Buchhalter greift auf fremden Mandanten zu | Zugriff wird verweigert | automatisiert | hoch |
| T-IAM-004 | API ohne Token im Produktivmodus | Zugriff wird verweigert | automatisiert | hoch |
| T-IAM-005 | API mit Pruefer-Token schreibt Daten | Zugriff wird verweigert | automatisiert | hoch |
| T-IAM-006 | MCP-Tool mit falschem Tenant | Zugriff wird verweigert | automatisiert | hoch |
| T-IAM-007 | Admin erstellt Benutzer | Benutzer wird angelegt und Audit-Log geschrieben | automatisiert | mittel |

## 8. Importe und Exporte

| ID | Testfall | Erwartung | Art | Prioritaet |
|---|---|---|---|---|
| T-EXP-001 | DATEV-Export einfache Buchung | Datei enthaelt korrekte Werte und Encoding | automatisiert | hoch |
| T-EXP-002 | DATEV-Export Splitbuchung | Splitbuchung bleibt nachvollziehbar | automatisiert | hoch |
| T-EXP-003 | DATEV-Export festgeschriebener Stapel | Festschreibekennzeichen wird korrekt gesetzt | automatisiert | hoch |
| T-EXP-004 | Prueferexport erzeugen | Paket enthaelt Manifest, Daten, Belegindex und Belege | automatisiert | hoch |
| T-EXP-005 | Prueferexport Hashes pruefen | Alle im Manifest genannten Hashes stimmen | automatisiert | hoch |
| T-EXP-006 | Export mit Zeitraum | Nur relevante Daten plus notwendige Stammdaten enthalten | automatisiert | mittel |
| T-EXP-007 | Bank-CSV importieren | Umsaetze werden korrekt gelesen und dedupliziert | automatisiert | mittel |
| T-EXP-008 | E-Rechnung importieren | Rechnungsdaten werden gelesen und korrekt verbucht | automatisiert | mittel |
| T-EXP-009 | Feldkatalog im Prueferexport | Alle exportierten Tabellen und Felder sind technisch beschrieben | automatisiert | hoch |
| T-EXP-010 | Export reproduzieren | Identischer Datenstand ergibt denselben Datenbestands-Hash | automatisiert | hoch |
| T-EXP-011 | Exportdatei manipulieren | Paketpruefung meldet Datei- und Datenbestands-Hashabweichung | automatisiert | hoch |
| T-EXP-012 | Benutzer und Rollen exportieren | Rollen sind enthalten; Passwort- und Token-Hashes fehlen | automatisiert | hoch |

## 9. Umsatzsteuer und Meldungen

| ID | Testfall | Erwartung | Art | Prioritaet |
|---|---|---|---|---|
| T-VAT-001 | UStVA Monat | Kennziffern werden aus Journaldaten korrekt berechnet | automatisiert | hoch |
| T-VAT-002 | UStVA Quartal | Zeitraum wird korrekt ausgewertet | automatisiert | hoch |
| T-VAT-003 | UStVA Jahr | Jahreswerte stimmen mit Journaldaten ueberein | automatisiert | mittel |
| T-VAT-004 | Storno in UStVA | Storno neutralisiert urspruengliche Buchung | automatisiert | hoch |
| T-VAT-005 | UStVA-Snapshot festhalten | Snapshot bleibt unveraendert trotz spaeterer Buchungen | automatisiert | hoch |

## 10. Anlagenbuchhaltung

| ID | Testfall | Erwartung | Art | Prioritaet |
|---|---|---|---|---|
| T-FA-001 | Lineare AfA | Plan und Buchung sind korrekt | automatisiert | hoch |
| T-FA-002 | Degressive AfA mit Wechsel | Automatischer Wechsel zur linearen AfA nachvollziehbar | automatisiert | mittel |
| T-FA-003 | Leistungs-AfA | Abschreibung folgt Leistungsmengen | automatisiert | mittel |
| T-FA-004 | GWG | Sofortabschreibung korrekt | automatisiert | mittel |
| T-FA-005 | Sammelposten | Fuenfjahresverteilung korrekt | automatisiert | mittel |
| T-FA-006 | Ausserplanmaessige AfA | Buchwert und Audit-Log korrekt | automatisiert | mittel |
| T-FA-007 | Anlagenabgang | Restbuchwert wird korrekt ausgebucht | automatisiert | hoch |

## 11. Betriebs- und Sicherheitspruefungen

| ID | Testfall | Erwartung | Art | Prioritaet |
|---|---|---|---|---|
| T-OPS-001 | Produktivstart ohne SECRET_KEY | Start wird verhindert oder klare Warnung | automatisiert | hoch |
| T-OPS-002 | Migration gegen bestehende DB | Migration laeuft oder bricht fail-fast ab | automatisiert | hoch |
| T-OPS-003 | Backup erzeugen | Datenbank und Belege sind vollstaendig gesichert | manuell | hoch |
| T-OPS-004 | Restore durchfuehren | System ist aus Backup wiederherstellbar | manuell | hoch |
| T-OPS-005 | Security-Header | Header sind gesetzt | automatisiert | mittel |
| T-OPS-006 | CSRF-Schutz | Formulare ohne Token werden abgewiesen | automatisiert | hoch |
| T-OPS-007 | Upload-Missbrauch | Path Traversal und gefaehrliche Inhalte werden verhindert | automatisiert | hoch |

## 12. Mindestkriterium fuer Release-Freigabe

Ein Compliance-Release darf nur freigegeben werden, wenn:

- alle Tests mit Prioritaet `hoch` erfolgreich sind oder begruendet ausgeschlossen wurden,
- die Testdaten und erwarteten Ergebnisse versioniert vorliegen,
- der CI-Lauf zum Release-Commit archiviert ist,
- manuelle Tests mit Datum und verantwortlicher Person dokumentiert sind,
- bekannte Abweichungen im Changelog und in den Release Notes genannt werden.
