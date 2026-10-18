# Compliance-Anforderungen fuer OpenBuchhaltung

Dieser Ordner beschreibt die Anforderungen, die OpenBuchhaltung erfuellen sollte, damit die Software von Steuerberatern, Wirtschaftspruefern und Betriebspruefern nachvollziehbar beurteilt werden kann.

Wichtig: Eine Software-Zertifizierung ersetzt nicht die ordnungsmaessige Einrichtung und Nutzung beim jeweiligen Unternehmen. Neben der Produktfunktionalitaet sind Betrieb, Berechtigungen, Backup, Verfahrensdokumentation und organisatorische Kontrollen beim Anwender relevant.

## Zielbild

OpenBuchhaltung soll eine pruefbare, GoBD-orientierte Buchhaltungssoftware fuer deutsche Kapitalgesellschaften werden. Fuer eine spaetere externe Softwarepruefung sollte eine eindeutig versionierte Produktversion mit festem Funktionsumfang, Datenbankschema, Dokumentation, Testnachweisen und Release-Artefakten bereitgestellt werden.

## Relevante Pruef- und Anforderungsbereiche

- GoBD-orientierte Nachvollziehbarkeit, Vollstaendigkeit, Richtigkeit, Ordnung und Unveraenderbarkeit
- HGB-orientierte Buchfuehrungs- und Abschlussfunktionen
- IDW-PS-880-Readiness fuer eine moegliche Softwarebescheinigung
- revisionssichere Belegablage und Beleg-Buchungs-Verknuepfung
- vollstaendige Protokollierung und Manipulationserkennung
- Datenzugriff und Export fuer Steuerberater, Wirtschaftspruefer und Betriebspruefer
- kontrollierter Entwicklungs-, Test-, Release- und Betriebsprozess

## Dokumente in diesem Ordner

- `gobd-kriterienkatalog.md` - fachliche und technische GoBD-Anforderungen mit Umsetzungshinweisen
- `idw-ps-880-readiness.md` - Vorbereitung auf eine moegliche Softwarepruefung
- `verfahrensdokumentation-outline.md` - Gliederung fuer eine Verfahrensdokumentation
- `testkatalog.md` - fachliche und technische Testfaelle fuer die Compliance-Haertung
- `produktionsbetrieb.md` - Mindestanforderungen an Installation, Betrieb und Verantwortlichkeiten

## Empfohlene naechste Schritte

1. Anforderungen in Issues oder Meilensteine ueberfuehren.
2. Bestehende Funktionen gegen den Kriterienkatalog mappen.
3. Offene Luecken priorisieren: Unveraenderbarkeit, Belegarchiv, Prueferexport, Testnachweise.
4. Eine Version `1.0-compliance-candidate` definieren.
5. Readiness Assessment durch einen IT-pruefungsnahen Wirtschaftspruefer einplanen.

## Statuslogik

Die Tabellen in den Detaildokumenten nutzen folgende Statuswerte:

- `offen` - noch nicht umgesetzt oder nicht nachgewiesen
- `teilweise` - Funktion vorhanden, aber technisch oder dokumentarisch noch nicht prueffest
- `umgesetzt` - Funktion vorhanden und durch Tests/Dokumentation belegbar
- `organisatorisch` - muss im Betrieb beim Anwender geregelt werden
