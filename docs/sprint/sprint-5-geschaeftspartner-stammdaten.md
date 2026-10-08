# Sprint 5a – Geschäftspartner-Stammdaten (Debitoren/Kreditoren) – Detailplan

- **Stand:** 2026-10-08; Entscheidungen aus Abschnitt 9 angenommen (ADR-002),
  PR 1 (Schritte 0–2) umgesetzt
- **Bezug:** Projektreview 2026-09-19, Feature #1 „Kunden-/Lieferantenstamm“
  (Phase 5, Sprint 5 – Produkt-Basis). Berührt #5 (Eingangsrechnung → OPOS),
  F3 (DATEV-Export) und F8 (OPOS/Zahllauf).
- **Vorlage im Code:** Kostenstellen/Profitcenter (`ControllingUnit`). Dort gibt es
  bereits Stammdaten mit Vorher-/Nachher-Historie, die Kontierung auf
  `JournalEntryLine`, Festschreibungs-Hashversion 2, Storno-Spiegelung,
  UI/API/MCP und den Prüferexport.

## 1. Ausgangslage

Es gibt kein Partnermodell. Partnerangaben stehen nur als Freitext an einzelnen
Stellen:

| Stelle | Heute | Fundstelle |
|---|---|---|
| Offene Posten | `counterparty` als Freitext; `counterparty_iban`/`_bic` werden von Hand erfasst | `OpenItem`, `app/services/open_items.py` |
| Bankumsätze | `counterparty` = Name aus dem Auszug; **keine** Gegen-IBAN. Der Name ist Teil des Dedup-Hashs | `bank_statement.py`, `bank_import.py` (`_dedup_hash`) |
| Zahllauf | Empfänger = OPOS-Freitext und OPOS-IBAN | `sepa_export.py` (`payable_items_for_run`, `create_payment_run`) |
| Mahnschreiben | Empfänger = Freitext; die Anschrift muss „von Hand eingetragen“ werden | `templates/mahnschreiben.html` |
| E-Rechnung Import | nur `seller_name`; keine USt-IdNr, Anschrift, IBAN oder Fälligkeit; es entsteht kein OPOS | `einvoice_import.py`, `incoming_invoice.py` |
| E-Rechnung Export | Käufer kommt aus Formularfeldern; CII schreibt nur den Käufernamen | `einvoice_export.py` (`Party`), `web/einvoice.py` |
| Beleg-OCR/-Abgleich | Lieferant = erste Textzeile oder LLM; der Abgleich nutzt nur Betrag und Datum | `receipt_ocr.py` (`_find_supplier`), `receipt_matching.py` |
| Buchungen | kein Partnerbezug; der Code erkennt Sammelkonten (1400/1600) nirgends als solche | `journal_entries.py` |
| DATEV-Export | nur Sachkonten; keine Personenkonten, kein Stammdatenexport | `datev_export.py` |

## 2. Ziel und Nicht-Ziele

**Ziel:** Ein Kunden- und Lieferantenstamm je Gesellschaft. An ihm hängen
Buchungen auf Sammelkonten, offene Posten, Zahllauf, Mahnwesen und E-Rechnung.
Jeder Partner hat Personenkontonummern, die im DATEV-Export als Debitor bzw.
Kreditor erscheinen.

**Nicht-Ziele dieses Sprints:**
- Ausgangsrechnungsmodul (#2)
- Skonto-Automatik (#11)
- § 13b und ZM (#4); diese nutzen später Land und USt-IdNr des Partners
- Firmenstammdaten in der DB (#3)
- Online-Prüfung der USt-IdNr beim BZSt
- Lastschriftmandate
- mehrere Anschriften oder Ansprechpartner je Partner
- Fremdwährung

## 3. Architekturentscheidung: Nebenbuch statt Personenkonten im Kontenplan

**Empfehlung (Option A): Partner als Nebenbuch-Dimension an der
Buchungszeile.** Im Hauptbuch wird weiter auf das Sammelkonto gebucht (z. B.
1400/1600). Die Zeile trägt zusätzlich `partner_id`. Die Personenkontonummer
(Debitor/Kreditor) ist ein Attribut des Partners und wird erst im DATEV-Export
als Konto ausgegeben.

**Verworfen (Option B): jeder Partner als eigenes `Account` (fünfstellig, ab
10000).** Das bricht an mehreren Stellen:
- `Account.derive_hierarchy` liest nur die ersten vier Ziffern. Dadurch hängt
  10010 unter 1000 (Kasse) und 70001 unter 7000.
- Die Summen-/Saldenliste sortiert die Kontonummern als Text, 10000 steht also
  zwischen 1000 und 1200.
- Die Bilanz zeigt eine Zeile je Konto statt einer Position „Forderungen aLuL“.
- Die Auswahllisten für Bankkonten zeigen alle `asset`-Konten. Jeder Debitor
  erschiene dort als Bankkonto.
- Die Geldtransit-Suche nach Name und Code könnte ein Personenkonto treffen.
- Der Kontenplan bekäme Hunderte Konten, und die Kontenpflege würde zur
  Partnerpflege.

**Folgen von Option A:**
- `partner_id` gehört in den Festschreibungs-Hash. Das erfordert Hashversion 3
  (siehe Abschnitt 4.3).
- Der Saldovortrag (`periods.py`) summiert heute je Konto. Er muss auf
  Sammelkonten je Konto und Partner vortragen, sonst gehen die Partnersalden beim
  Jahreswechsel verloren.
- Der DATEV-Export braucht ein Mapping von Sammelkonto und Partner auf das
  Personenkonto (Schritt 6).

Die Entscheidung wird als `docs/adr/ADR-002-geschaeftspartner-nebenbuch.md`
festgehalten.

## 4. Datenmodell

### 4.1 Neue Tabelle `business_partner` (Modell `BusinessPartner`)

| Feld | Typ | Regel |
|---|---|---|
| `id`, `tenant_id`, `company_id` | FK | wie bei allen Stammdaten gesellschaftsbezogen |
| `debtor_number` | String(20), null | gesetzt = Kunde; Bereich 10000–69999; eindeutig je Gesellschaft |
| `creditor_number` | String(20), null | gesetzt = Lieferant; Bereich 70000–99999; eindeutig je Gesellschaft |
| `partner_kind` | `organization` \| `person` | für den DATEV-Adressatentyp und den Datenschutz |
| `name` | String(255) | Pflicht |
| `street`, `postal_code`, `city` | String, null | |
| `country_code` | String(2) | ISO 3166-1 alpha-2, Default `DE` |
| `vat_id` | String(20), null | USt-IdNr, mit Formatprüfung |
| `tax_number` | String(30), null | Steuernummer |
| `email`, `phone`, `contact_person` | String, null | |
| `iban`, `bic` | String(34) / String(11), null | nur über einen eigenen Endpunkt änderbar (siehe 5.9) |
| `payment_term_days` | Integer, null | ≥ 0; Default für die OPOS-Fälligkeit |
| `notes` | Text, null | |
| `is_active` | Boolean | Löschen ist nicht vorgesehen |
| `created_at` | DateTime | |

Constraints:
- `UNIQUE(company_id, debtor_number)` und `UNIQUE(company_id, creditor_number)`
- CHECK: mindestens eine Rolle ist gesetzt
- CHECK: `payment_term_days >= 0`
- CHECK: zulässige Werte für `partner_kind`

### 4.2 Erweiterungen bestehender Tabellen

| Tabelle | Neue Spalte | Zweck |
|---|---|---|
| `account` | `subledger` (`debtor` / `creditor` / null) | Sammelkonto-Kennzeichen; nur auf solchen Konten ist ein Partner zulässig |
| `journal_entry_line` | `partner_id` (FK RESTRICT, null, Index) | Nebenbuch |
| `open_item` | `partner_id` (FK RESTRICT, null, Index) | offene Posten am Partner |
| `bank_transaction` | `counterparty_iban` (Schritt 5) | Partnererkennung; **nicht** Teil des Dedup-Hashs |
| `bank_booking_rule` | `partner_id` (Schritt 5, optional) | eine Regel setzt den Partner |

### 4.3 Migration und Hashversion 3

**Revision:**
- Die Revision hinter den aktuellen Head von `origin/main` hängen. Stand heute
  ist das `20261008_0041_oauth`, die neue Revision wäre also
  `…_0042_business_partner`.
- `alembic heads` muss danach genau einen Head zeigen.

**Neue Spalten:**
- SQLite: per `ALTER TABLE … ADD COLUMN … REFERENCES`, ohne Tabellen-Rebuild.
  Ein Rebuild würde die Immutability-Trigger entfernen. Muster:
  `20260714_0023_controlling_dimensions.py`.
- PostgreSQL: per `op.add_column`.

**Hashversion 3:**
- `JOURNAL_CONTENT_HASH_VERSION = 3` nimmt `partner_id` je Zeile auf.
- **Kein Re-Hashing.** Die Verifikation akzeptiert die Versionen 2 und 3 und
  rechnet je Version mit dem passenden Feldsatz. Bereits festgeschriebene
  Buchungen behalten ihr Siegel unverändert.
- Dafür müssen der CHECK `ck_journal_entry_finalized_content_hash` und die
  SQLite-Trigger `obk_journal_entry_hash_required_on_*` auf `IN (2, 3)`
  umgestellt werden.
- Verifikation und Prüferexport müssen beide Versionen nachvollziehbar ausweisen.

**Sammelkonto-Kennzeichen für Bestandsdaten** (geändert bei der Umsetzung):
- Die Migration setzt das Kennzeichen **nicht**. Es wird über die Kontenpflege
  gesetzt, damit jede Änderung mit Vorher-/Nachher-Snapshot im Audit-Log steht.
- Der Kontenrahmen-Import erkennt „Forderungen/Verbindlichkeiten aus Lieferungen
  und Leistungen“ an der Bezeichnung oder nimmt eine optionale Spalte
  `subledger`. Die mitgelieferten CSV-Dateien bleiben unverändert.

**Test:** Downgrade sowie Upgrade → Downgrade → Upgrade im Rundlauf. Danach
müssen die Immutability-Trigger noch aktiv sein.

## 5. Fachregeln

1. **Rollen und Nummern.**
   - Kunde heißt: eine Debitorennummer ist gesetzt. Lieferant heißt: eine
     Kreditorennummer ist gesetzt. Ein Partner kann beides sein.
   - Ohne Eingabe vergibt der Service die nächste freie Nummer im Bereich
     (DATEV-Standard bei vierstelligen Sachkonten).
   - Manuelle Nummern sind nur innerhalb des Bereichs zulässig.
   - Eine Nummer wird unveränderlich, sobald sie in einer Buchungszeile oder
     einem OPOS verwendet wurde.
   - Eine Rolle lässt sich nur entfernen, solange sie nicht verwendet wurde.
2. **Kein Löschen.**
   - Partner werden deaktiviert statt gelöscht.
   - Inaktive Partner sind für neue Buchungen und OPOS gesperrt.
   - Bestehende OPOS eines inaktiven Partners bleiben ausgleichbar.
3. **Buchungszeile.**
   - `partner_id` ist nur auf Konten mit `subledger` zulässig.
   - Die Rolle muss zum Sammelkonto passen: Debitor-Sammelkonto → Kunde,
     Kreditor-Sammelkonto → Lieferant.
   - Der Partner muss zur Gesellschaft gehören und aktiv sein.
   - Zeilen ohne Partner bleiben zulässig (Altbestand, Sammelbuchungen). Eine
     Partnerpflicht je Gesellschaft ist ein späterer Schalter.
   - Automatische USt-/VSt-Zeilen bekommen keinen Partner.
   - Ein Storno spiegelt `partner_id`.
4. **Sammelkonto-Kennzeichen.**
   - Es wird über die bestehende Kontenänderung gepflegt, mit
     Vorher-/Nachher-Historie.
   - Es lässt sich nur entfernen, solange keine Zeile mit Partner auf dem Konto
     steht.
5. **OPOS mit Partner.**
   - Die Rolle muss zu `item_type` passen.
   - Das Konto muss ein Sammelkonto des passenden Typs sein.
   - Vorbelegung als Snapshot:
     - `counterparty` = Name des Partners
     - IBAN/BIC aus dem Partner
     - `due_date` = Belegdatum plus Zahlungsziel
   - Der Freitext bleibt für Altbestand und Einmalpartner erhalten.
6. **Validierung.**
   - IBAN/BIC über die vorhandenen Funktionen `normalize_iban` und
     `normalize_bic`.
   - USt-IdNr mit Länderpräfix: für DE `DE` plus 9 Ziffern, für andere Länder
     ein generisches Muster.
   - Land als ISO-Code.
7. **Dubletten.** Es gibt einen Hinweis, aber keine Sperre, bei gleicher
   USt-IdNr, gleicher IBAN oder gleichem normalisiertem Namen.
8. **Audit.**
   - `business_partner` erzeugt `created` und `updated` mit vollständigem
     `before`/`after` (Muster: `controlling.py`).
   - Eine Änderung der Bankdaten ist eine eigene Aktion `bank_details_changed`.
   - Updates ohne Änderung erzeugen keinen Eintrag.
9. **Sicherheit.**
   - Bankdaten lassen sich nur über einen eigenen Endpunkt ändern.
   - Das zugehörige MCP-Tool ist im Chat gesperrt, wie bereits
     `set_company_bank_details`. So kann eine präparierte Rechnung im Chat keine
     Lieferanten-IBAN umbiegen.
   - Schreiben dürfen nur `admin` und `buchhalter`; Prüfer und Support lesen.
10. **Datenschutz.**
    - Partner können natürliche Personen sein.
    - Eine Löschung kollidiert mit den Aufbewahrungspflichten. Im MVP gibt es
      daher nur das Deaktivieren.
    - Die Anonymisierung nach Ablauf der Frist wird als Folgepunkt in der
      Verfahrensdokumentation vermerkt.

## 6. Schnittstellen (Parität UI / REST / MCP / Chat)

| Funktion | UI | REST (`/api/v1`) | MCP-Tool | Chat |
|---|---|---|---|---|
| Partner listen/suchen (Rolle, `q`, inaktive) | Seite „Partner“ | `GET /partners` | `list_partners` | sofort |
| Partner anzeigen | Detailansicht | `GET /partners/<id>` | `get_partner` | sofort |
| Partner anlegen | Formular | `POST /partners` | `create_partner` | mit Bestätigung |
| Partner ändern / deaktivieren | Formular | `PATCH /partners/<id>` | `update_partner` | mit Bestätigung |
| Bankdaten setzen | Formular | `PUT /partners/<id>/bank-details` | `set_partner_bank_details` | **gesperrt** |
| Änderungshistorie | Detailansicht | `GET /partners/<id>/history` | `get_partner_history` | sofort |
| Partnerkonto (Zeilen, laufender Saldo, offene Posten) | Detailansicht | `GET /partners/<id>/statement` | `get_partner_statement` | sofort |
| Saldenliste Debitoren/Kreditoren mit Abstimmung | Berichte | `GET /reports/partner-balances` | `get_partner_balances` | sofort |
| DATEV-Stammdatenexport | Berichte | `GET /exports/datev-partners.csv` | `export_datev_partners_csv` | sofort |

Der Menüpunkt „Partner“ kommt in die Gruppe „Buchen“, neben „Konten“ und
„Controlling“.

Diese bestehenden Endpunkte bekommen `partner_id` als Feld bzw. Filter:
- Buchung anlegen und listen
- OPOS anlegen und listen
- Zahllauf-Vorschläge
- Bankbuchung
- E-Rechnung-Export

## 7. Lieferreihenfolge

| Schritt | Inhalt | Aufwand |
|---|---|---|
| 0 | Vorarbeiten | S |
| 1 | Partner-Stammdaten | M |
| 2 | Sammelkonten und Partner an der Buchungszeile | M |
| 3 | OPOS, Bank, Zahllauf, Mahnwesen | M |
| 4 | Partnerkonto und Saldenliste | S–M |
| 5 | Partnererkennung in automatischen Quellen | M–L |
| 6 | DATEV-Personenkonten und Stammdatenexport | M |
| 7 | Altdatenübernahme | S |

**PR-Schnitt:**
- PR 1: Schritte 0–2
- PR 2: Schritte 3–4
- PR 3: Schritt 5
- PR 4: Schritte 6–7 (Schritt 6 erst nach F3)

Für jeden PR gilt: pytest und ruff grün, Live-Verifikation gegen die laufende
App, CHANGELOG `[Unreleased]` ergänzt, `docs/umsetzungsplan.md` (Phase 5,
Sprint 5) abgehakt.

### Schritt 0 – Vorarbeiten (S)
- [x] Kontotypen `receivable`/`payable` aus `konten.html` entfernen.
  - Grund: Konten mit diesen Typen fallen heute aus der Bilanz (sie wird dann
    unausgeglichen) und aus dem Saldovortrag (`reports.py`, `periods.py`).
  - Ersatz ist das Sammelkonto-Kennzeichen.
  - Die API lehnt unbekannte Kontotypen für neue Konten ab.
- [x] Bestandsprüfung auf der webbox: keine Konten mit `receivable`/`payable`,
  aber Konto 4240 der unikin GmbH mit Kontoart „Strom“ (ungequotete Zeile
  `4240,Gas, Strom, Wasser,expense` in `skr04.csv`).
  - Statt eines CLI-Kommandos: Die Kontoart lässt sich in UI/API/MCP
    reparieren, aber nur wenn der bisherige Wert ungültig ist (mit
    Audit-Eintrag). Der Import lehnt unbekannte Kontoarten und Zeilen mit zu
    vielen Feldern ab.
- [x] ADR-002 anlegen.

### Schritt 1 – Partner-Stammdaten (M)
- [x] Modell `BusinessPartner` und Migration (nur die neue Tabelle, siehe 4.1).
- [x] Service `app/services/partners.py`:
  - anlegen, ändern, deaktivieren
  - Nummernvergabe mit Retry bei Kollision
  - Validierungen und Dublettenhinweise
  - Historie und Bankdaten-Setter
- [x] UI `partner.html`: Liste mit Filter und Pagination, Anlage, Bearbeiten,
  Detail mit Historie.
- [x] API `app/api/partners.py` mit `api_can_write()`, Mandantentrennung und
  Pagination.
- [x] MCP:
  - ToolSpecs anlegen und in `EXPECTED_TOOL_NAMES` eintragen.
  - `set_partner_bank_details` in `CHAT_BLOCKED_TOOL_NAMES` und
    `EXPECTED_BLOCKED_TOOLS` aufnehmen.
- [x] Prüferexport:
  - `business_partners` und `business_partner_history` in
    `EXPORT_TABLE_MODELS` und `TABLE_DESCRIPTIONS` registrieren.
  - Feldkatalog `docs/compliance/prueferexport-datenbeschreibung.md`
    nachziehen.
  - `gobd-kriterienkatalog.md` AUD-003 („weitere Stammdaten folgen“)
    nachziehen.
- [x] seed-demo: drei Kunden und drei Lieferanten anlegen (einer davon in
  beiden Rollen).

### Schritt 2 – Sammelkonten und Partner an der Buchungszeile (M)
- [x] Migration: `account.subledger`, `journal_entry_line.partner_id`,
  Hash-CHECK und Trigger (siehe 4.3).
- [x] Konten:
  - Kennzeichen in UI, API und MCP (`update_account`), mit Historie.
  - Der Import setzt das Kennzeichen (Bezeichnung oder optionale Spalte).
  - Sammelkonten erscheinen nicht mehr als Bankkonten.
- [x] Buchungsservice:
  - `JournalLineInput.partner_id`
  - Validierung neben `validate_controlling_assignment`
  - Insert und Storno-Spiegelung
  - Hash v3 und Verifikation für v2 und v3
- [x] Buchungsmaske:
  - Partnerauswahl je Zeile, gefiltert nach Sammelkonto und Rolle. Das JS kommt in
    `app.js`, da die CSP keine Inline-Skripte erlaubt.
  - Die Journal-Anzeige zeigt Partnernummer und -name.
- [x] API/MCP `create_journal_entry`:
  - `partner_id` (alternativ `partner_number`) je Zeile.
  - Das Zeilenschema erweitern (es hat `additionalProperties: false`).
- [x] Buchungsvorlagen: `partner_id` im Zeilen-JSON.
- [x] Eröffnungsbilanz: optionale Spalte Partnernummer
  (`Konto;Soll;Haben;Partner`), damit offene Salden übernommen werden können.
- [x] Saldovortrag: Sammelkonten je Konto und Partner vortragen.
- [x] Journal-CSV: Spalten `partner_number` und `partner_name` hinten anhängen,
  damit die bisherigen Spaltenpositionen bleiben.

### Schritt 3 – OPOS, Bank, Zahllauf, Mahnwesen (M)
- [ ] Migration `open_item.partner_id`.
- [ ] OPOS:
  - Anlage mit Partnerauswahl und Vorbelegung (siehe 5.5).
  - Filter `partner_id`.
  - Die Suche `q` durchsucht auch Partnername und -nummer.
- [ ] Bankumsatz verbuchen:
  - Ist das Gegenkonto ein Sammelkonto, gibt es eine Partnerauswahl.
  - Beim OPOS-Ausgleich wird geprüft, ob der Partner der Zahlung zum Partner
    des Postens passt (sofern beide gesetzt sind).
- [ ] Zahllauf:
  - IBAN aus dem OPOS-Snapshot, ersatzweise vom Partner.
  - Warnung, wenn die OPOS-IBAN von der aktuellen Partner-IBAN abweicht.
  - Empfängername aus dem Partner.
- [ ] Mahnschreiben: Die Empfängeranschrift kommt aus dem Partner statt aus
  Leerzeilen. Die Absenderanschrift bleibt bis #3 bei `SELLER_*`.

### Schritt 4 – Partnerkonto und Saldenliste (S–M)
- [ ] Partnerkonto:
  - die Sammelkontozeilen des Partners mit Anfangssaldo und laufendem Saldo
  - getrennt nach Debitor- und Kreditorseite
  - dazu die offenen Posten
- [ ] Saldenliste Debitoren/Kreditoren zum Stichtag, mit einer Abstimmungszeile
  je Sammelkonto: Summe der Partnersalden + Zeilen ohne Partner = Saldo des
  Sachkontos. Abweichungen zwischen OPOS und Hauptbuch (F8) werden so sichtbar.
- [ ] CSV-Download. Die Dashboard-Kachel „Überfällige Posten“ verlinkt auf den
  Partner.

### Schritt 5 – Partnererkennung in automatischen Quellen (M–L)
- [ ] E-Rechnung-Import:
  - Zusätzlich parsen: USt-IdNr (BT-31), Anschrift, IBAN (BT-84) und
    Fälligkeit (BT-9).
  - Partner suchen in dieser Reihenfolge: USt-IdNr, IBAN, normalisierter Name.
    Ohne Treffer: „Lieferant anlegen“ mit vorbefüllten Daten.
  - Die Kreditorzeile bekommt den Partner, und ein OPOS mit Fälligkeit wird
    angelegt. Damit ist Review #5 abgedeckt.
- [ ] Beleg-OCR und Belegabgleich:
  - IBAN und USt-IdNr per Regex erkennen und einen Partner vorschlagen.
  - Gebucht wird auf demselben Weg wie bei der E-Rechnung.
- [ ] Bank:
  - `counterparty_iban` übernehmen: aus CAMT (`RltdPties/…Acct`), aus MT940
    (`gvc_applicant_iban`) und über einen CSV-Alias. Nicht in den Dedup-Hash.
  - Partnervorschlag per IBAN.
  - OPOS-Vorschlag nach Partner und Betrag.
- [ ] Kontierungsregeln können optional einen Partner setzen.
- [ ] E-Rechnung-Export:
  - Der Käufer kommt aus dem Partner (Anschrift, USt-IdNr).
  - CII um Käuferanschrift und Käufer-USt-IdNr ergänzen; heute wird nur der
    Name geschrieben.

### Schritt 6 – DATEV (M, nach oder zusammen mit F3)
- [ ] Buchungsstapel: Eine Sammelkontozeile mit Partner wird mit der
  Personenkontonummer als Konto ausgegeben.
- [ ] Stammdatenexport Debitoren/Kreditoren (EXTF, Datenkategorie 16). Die
  Feldbelegung gegen die DATEV-Formatbeschreibung prüfen.
- [ ] Golden-File-Tests.

### Schritt 7 – Altdatenübernahme (S)
- [ ] Vorschlagsliste aus `OpenItem.counterparty` (und IBAN), gruppiert nach
  normalisiertem Namen.
  - Die Übernahme legt Partner an und verknüpft die OPOS über `partner_id`.
  - Der Freitext bleibt unverändert.
- [ ] UI/API/MCP. Festgeschriebene Buchungen sind unveränderlich und bleiben
  ohne Partner; die Zuordnung läuft nur über die OPOS.

## 8. Tests

- **Service:**
  - Nummernvergabe, Bereiche und Kollision
  - Rollenentzug bei bestehender Verwendung
  - Validierung von IBAN und USt-IdNr
  - Update ohne Änderung, Historie
- **API:**
  - Rechte: Prüfer und Support lesen, Schreibrollen schreiben
  - Mandantentrennung: eine fremde `partner_id` wird abgewiesen
  - Pagination und Bankdaten-Endpunkt
- **MCP:** `EXPECTED_TOOL_NAMES`, Weitergabe der Argumente,
  `test_tools_cover_all_api_endpoints`.
- **Chat:** `set_partner_bank_details` ist gesperrt; `create_partner` und
  `update_partner` brauchen eine Bestätigung.
- **Buchung:**
  - Partner nur auf Sammelkonten, Rollenprüfung
  - Storno spiegelt den Partner
  - Hash v3; bestehende v2-Siegel verifizieren weiterhin
  - Trigger blockieren Änderungen an festgeschriebenen Zeilen
- **Abschluss:** Saldovortrag je Partner; Abstimmung in der Saldenliste.
- **Migration:**
  - Upgrade, Downgrade und Upgrade auf SQLite mit Triggern
  - PostgreSQL, sobald die CI-Matrix (T1) steht
- **Prüferexport:** neue Tabellen und Feldkatalog.
- **DATEV:** Golden Files.

## 9. Offene Entscheidungen (Vorschlag jeweils fett)

1. Nebenbuch oder Personenkonten als Konten? → **Nebenbuch (Option A).**
2. Partner nur auf Sammelkonto-Zeilen oder auf jeder Zeile? → **Nur auf
   Sammelkonten.** Auswertungen wie „Umsatz je Kunde“ laufen über die Buchung
   (Erlöszeilen derselben Buchung).
3. Hash: Version 3 neben Version 2, oder alle Siegel neu berechnen? → **v3 neben
   v2, kein Re-Hashing.**
4. Nummernkreise? → **DATEV-Standard 10000–69999 / 70000–99999 mit
   automatischer Vergabe.**
5. Partnerpflicht auf Sammelkonten? → **Zunächst optional**, der Schalter kommt
   später.
6. Bankdaten im Chat? → **Gesperrt.**

## 10. Risiken

- **Hashversion 3** berührt die Compliance-Kernlogik. Sie braucht eigene Tests,
  und Verifikation sowie Prüferexport müssen beide Versionen ausweisen.
- **SQLite-Migration:** Wegen der Immutability-Trigger kein Tabellen-Rebuild.
- **Parallele Sprints:** Konflikte bei der Migrationsnummer und im
  Umsetzungsplan.
- **SKR04-Kontenrahmen:** Mit #155 korrigiert (Forderungen aLuL 1200,
  Verbindlichkeiten aLuL 3300); der Import kennzeichnet beide als Sammelkonten.
  Gesellschaften mit Altbestand aus dem fehlerhaften Import (u. a. die
  unikin GmbH) meldet die Kontenrahmen-Prüfung; dort die Sammelkonten auf den
  tatsächlich genutzten Forderungs-/Verbindlichkeitskonten kennzeichnen.
- **DATEV-Formatdetails** (Kategorie 16) sind ohne offizielle Prüfung nur
  „kompatibel“, nicht zertifiziert.
- **Umgeleitete Zahlungen:** `create_open_item` kann im Chat (nach Bestätigung)
  schon heute eine IBAN setzen. Mit dem Partner-Fallback im Zahllauf ist zu
  prüfen, ob dieses Feld im Chat ebenfalls gesperrt werden sollte.
