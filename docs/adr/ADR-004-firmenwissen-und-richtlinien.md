# ADR-004: Firmenwissen und Unternehmensrichtlinien je Gesellschaft

- **Status:** Angenommen (Stufe 1 „Firmenwissen“ umgesetzt, Stufe 2 „Richtlinien“ geplant)
- **Datum:** 2026-10-31
- **Entscheider:** OpenBuchhaltung Kernteam

## Kontext
Obi kennt seit der Wissensbasis `data/wissen/*.md` das allgemeine Fachwissen mit
Rechtsstand (Umsatzsteuer, GoBD, Kontierung, Freigabe-Muster). Was Obi nicht kennt,
ist das Wissen des einzelnen Unternehmens: interne Richtlinien, Arbeitsanweisungen,
Kontierungsgewohnheiten, Stammdatenlisten. Solche Unterlagen liegen oft an mehreren
Orten und in mehreren Fassungen, teils veraltet, teils nur als Notiz.

Das vorhandene `KNOWLEDGE_EXTRA_DIR` hilft dabei nur begrenzt: Es ist ein Verzeichnis
auf dem Server, gilt für die ganze Instanz statt je Gesellschaft, kennt keinen Status,
keine Freigabe, keine Fundstelle, keine Wiedervorlage und kein Audit.

Unternehmenswissen ist vertraulich und gehört nie in das öffentliche Repository. Es muss
in der Datenbank der Instanz liegen, mandantengetrennt wie die Buchhaltungsdaten.

Zwei Arten von Unternehmensregeln sind zu unterscheiden:

- **Wissen**, das Obi finden, zitieren und beim Antworten berücksichtigen soll
  („Büromaterial bestellt das Sekretariat gesammelt“, Fundstelle Handbuch Seite 3). Es
  wirkt über das Modell und ist nicht technisch erzwingbar.
- **Richtlinien**, die das System durchsetzen soll („Zahlungen ab einem Betrag nur mit
  Zweitfreigabe“). Sie brauchen feste, typisierte Felder und eine Prüfung am
  Bestätigungs-Hook des Chats bzw. in den Services.

## Entscheidung
Drei getrennte Schichten:

| Schicht | Inhalt | Wirkung | Umsetzung |
|---|---|---|---|
| Firmenwissen | Quellen, Quellenauszüge, Wissenseinträge | wird gefunden und zitiert, nicht erzwungen | Stufe 1 (dieses ADR) |
| Richtlinien | Freigaberegeln, Grenzen, Kontierungsregeln | wird technisch durchgesetzt | Stufe 2 |
| Stammdaten | Kostenstellen, Profitcenter, Partner-Standards | Auswahl und Vorschläge | vorhanden |

### Stufe 1: Firmenwissen

**Datenmodell** (je Gesellschaft, `tenant_id` + `company_id` wie alle Fachtabellen):

- `company_knowledge_source` – eine Quelle (Richtlinie, Arbeitsanweisung, Systemanleitung,
  Stammdatenliste, Notiz): stabiler Schlüssel `source_key`, Titel, Herkunft (Pfad oder
  Verweis), SHA-256 des Originals, Dokumentart, Fassung/Stand, Gültigkeit von/bis,
  Verbindlichkeit (`binding` maßgeblich, `reference` Orientierung, `historical`
  historisch, `unverified` ungeprüft), Prüfhinweis, aktiv/entfallen, Herkunftspaket.
- `company_knowledge_excerpt` – ein Quellenauszug mit Fundstelle (Seite, Abschnitt,
  Blatt/Zeilen) und Text; dient Obi als Zitat.
- `company_knowledge_entry` – ein Wissenseintrag: Art (`knowledge` oder
  `open_question` für bekannte Lücken und Widersprüche), Themenfeld, Titel, Inhalt,
  Quelle und Fundstelle, Status `draft` → `approved` → `retired`, Wiedervorlage
  (`review_due`, `review_note`), Prüfgrund (`review_reason`, z. B. „Quelle geändert“),
  wer vorgeschlagen und wer freigegeben hat.

**Regeln:**

1. Importierte und von Obi vorgeschlagene Einträge sind immer Entwürfe. Die Freigabe
   (`approved`) setzt ein Mensch mit Schreibrolle über UI, REST-API oder MCP; im KI-Chat
   sind Freigabe, Statuswechsel, Quellen-Deaktivierung und Import gesperrt.
2. Ändert jemand Titel, Inhalt oder Fundstelle eines freigegebenen Eintrags, fällt er
   auf Entwurf zurück. Wiedervorlage und Themenfeld lassen sich ohne neue Freigabe ändern.
3. Nichts wird gelöscht: Einträge werden `retired`, Quellen und Auszüge inaktiv. Jede
   Änderung läuft mit Vorher-/Nachher-Werten über die Audit-Hashkette des Mandanten.
4. „Zu prüfen“ ist jeder nicht ausgemusterte Eintrag, der Entwurf ist, einen Prüfgrund
   trägt oder dessen Wiedervorlage erreicht ist.

**Wissenspaket (Austauschformat):** JSON mit `format: "openbuchhaltung-firmenwissen"`,
`version: 1`, Paketkopf (`key`, `revision`, `title`) sowie Listen `sources`, `excerpts`
und `entries` mit stabilen Schlüsseln (Beispiel im README, Abschnitt „Firmenwissen je
Gesellschaft“). Ein Paket wird vollständig geprüft, bevor etwas geschrieben wird; Schlüssel,
die zu einem anderen Paket oder zu manuell angelegten Daten gehören, brechen den Import ab. Der Import gleicht je Paket über diese Schlüssel
ab und kann als Vorschau (`dry_run`) laufen:

- neu → anlegen (Einträge als Entwurf);
- geändert → aktualisieren; ein freigegebener Eintrag mit geändertem Inhalt fällt auf
  Entwurf zurück; hat sich die Prüfsumme einer Quelle geändert, erhalten die daraus
  abgeleiteten freigegebenen Einträge den Prüfgrund „Quelle geändert“;
- im Paket entfallen → Eintrag `retired`, Quelle/Auszug inaktiv (nur innerhalb desselben
  Pakets; manuell angelegte Einträge und andere Pakete bleiben unberührt);
- von Hand ausgemusterte Einträge und von Hand deaktivierte Quellen bleiben es, auch wenn
  sie im Paket stehen; nur was der Import selbst ausgemustert bzw. deaktiviert hat, nimmt
  ein späterer Import wieder auf (als Entwurf).

Wie ein Paket entsteht (Export aus einem Wiki, Aufbereitung von PDFs und Tabellen, eigenes
Werkzeug), liegt außerhalb von OpenBuchhaltung. Formate Dritter werden vor dem Import in
das Paketformat überführt; dafür gibt es bewusst keinen fremdformatspezifischen Code im
Repository. Das Original einer Quelle bleibt maßgeblich; das Paket enthält Auszüge,
nicht die Dateien.

**Wirkung im Chat:**

- `search_knowledge` erhält `company_id` und liefert neben den Abschnitten der
  allgemeinen Wissensbasis die Treffer im Firmenwissen der Gesellschaft (Einträge und
  Quellenauszüge) mit Status, Quelle, Fundstelle und Gültigkeit. Gesucht wird mit
  demselben BM25-Verfahren; der Index entsteht je Gesellschaft bei Bedarf und wird bei
  Änderungen neu aufgebaut. Freigegebene Einträge werden höher gewichtet, Entwürfe sind
  ausdrücklich als „nicht freigegeben“ gekennzeichnet, ausgemusterte Einträge und
  inaktive Quellen werden nicht gefunden.
- Der Systemprompt nennt nur, ob die Gesellschaft Firmenwissen hat (Anzahl freigegebener
  Einträge und Entwürfe) und wie es zu gewichten ist; Inhalte kommen ausschließlich über
  das Tool, als Daten markiert wie alle Tool-Ergebnisse.
- Rangfolge beim Antworten: Gesetz/Rechtsstand → freigegebenes Firmenwissen → Entwurf
  bzw. Quellenauszug ohne Freigabe. Ein Firmeneintrag erlaubt Obi keine Aktion.

**Zugriff:** wie Buchhaltungsdaten der Gesellschaft. Lesen alle Rollen mit Zugriff auf
die Gesellschaft, Schreiben Admin und Buchhalter; Prüfer und Support nur lesend.

**Parität:** UI (Verwaltung → Firmenwissen: Liste mit Filtern, Eintrag anlegen/ändern,
freigeben/ausmustern, Quellen mit Auszügen, Import mit Vorschau; Seite „Wissen“ mit
Firmentreffern), REST-API unter `/api/v1/companies/<id>/knowledge/…`,
`/api/v1/knowledge-entries/<id>` und `/api/v1/knowledge-sources/<id>`, MCP-Tools
`list_/get_/create_/update_company_knowledge_entry`,
`set_company_knowledge_entry_status`, `list_/get_company_knowledge_source(s)`,
`set_company_knowledge_source_active`, `import_company_knowledge`.

### Stufe 2: Richtlinien (geplant)

`company_policy` mit Versionen (`draft` → `approved` → `retired`, Gültigkeit, Vorgänger),
Typen mit festen Feldern statt einer freien Regelsprache (`persona`, `guideline`,
`approval` mit Betragsschwellen, Tool-Familien je Rolle und Zweitfreigabe, später
`booking_rule`). Eine Prüfung `evaluate_tool_call` am Bestätigungs-Hook des Chats liefert
ausführen/bestätigen/Zweitfreigabe/ablehnen mit Regelgrund und Version; die
Code-Blockliste bleibt das nicht überschreibbare Minimum. Ein freigegebener
Wissenseintrag kann Ausgangspunkt einer Richtlinie sein, wird aber nie automatisch zu
einer. Freigaberegeln wandern später in eine zentrale Policy-Prüfung der Services, damit
UI, API, MCP und Chat dieselbe Regel sehen.

## Konsequenzen
- Positiv: Unternehmenswissen liegt mandantengetrennt, versioniert und auditierbar in
  der Instanz statt in Dateien oder im Repository; Obi kann Firmenregeln mit Fundstelle
  zitieren und Entwürfe von freigegebenem Wissen unterscheiden.
- Positiv: Ein Wissenspaket lässt sich wiederholt einspielen; Änderungen werden sichtbar
  und lösen eine erneute Prüfung aus, statt still zu überschreiben.
- Negativ: Firmenwissen wirkt nur über das Modell. Was erzwungen werden muss, braucht
  Stufe 2.
- Negativ: Der Suchindex liegt je Gesellschaft im Arbeitsspeicher jedes Workers; bei sehr
  großen Beständen (zehntausende Auszüge) wäre eine Datenbank-Volltextsuche nötig.
- Offene Punkte: Vier-Augen-Prinzip bei der Freigabe (mit `approval_request` aus Stufe 2),
  freigegebene Kerneinträge als eigener Prompt-Block, Abruf aus Wikis und Ablagen über
  Konnektoren, Firmenwissen im Prüferexport.

## Alternativen
- **`KNOWLEDGE_EXTRA_DIR` je Instanz weiter nutzen:** ohne Status, Freigabe, Fundstelle,
  Audit und Mandantentrennung; für vertrauliche, sich ändernde Unterlagen ungeeignet.
- **Vektordatenbank mit Embeddings:** braucht ein Embedding-Modell und eine weitere
  Komponente; die lexikalische Suche reicht für kuratierte Bestände von einigen tausend
  Abschnitten und bleibt nachvollziehbar.
- **Unterlagen als Belege in der Belegablage:** Belege sind Buchungsnachweise mit
  Aufbewahrungsregeln; Richtlinien und ihre Auszüge brauchen Status und Fundstellen statt
  Buchungsbezug.
- **Richtlinien und Wissen in einer Tabelle:** vermischt Durchsetzbares mit Zitierbarem;
  die Freigabe eines Wissenseintrags darf keine technische Wirkung entfalten.
