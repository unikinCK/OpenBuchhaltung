# Task P1-002: Belegupload + Verknüpfung mit Buchungen

## Ziel
Einen revisionsnahen Belegprozess bereitstellen: Upload von Belegen, Metadatenhaltung und Verknüpfung mit `JournalEntry`.

## Kontext
Im aktuellen Stand existiert der Buchungsflow ohne vollständigen Dokumentenprozess. Für GoBD-Nachvollziehbarkeit ist die Belegreferenz zentral.

## Abhängigkeit
- **Input aus:** `P1-000 Datenmodell v0`
- **Input aus:** vorhandenem JournalEntry-Flow

## Scope
- Upload-Endpunkt/UI für Belegdateien (MVP: PDF/JPG/PNG)
- Speicherung (lokales Filesystem für Dev, abstrahierte Storage-Schnittstelle)
- Persistenz in `Document` inkl. Prüfsumme/Metadaten
- Verknüpfung `Document` ↔ `JournalEntry`
- Optionaler Aufruf eines externen LLMs für Beleg-Updates über eine OpenAI-Responses-kompatible Schnittstelle
- Anzeige verknüpfter Belege in der Buchungsansicht
- Audit-Log-Einträge für Upload und Verknüpfung

## Akzeptanzkriterien
1. Ein Beleg kann erfolgreich hochgeladen und als `Document` gespeichert werden.
2. Ein Beleg kann einer bestehenden Buchung zugeordnet werden.
3. Doppelte Uploads identischer Datei werden erkennbar behandelt (mindestens Hinweis/Hash-Vergleich).
4. Upload und Verknüpfung erzeugen nachvollziehbare Audit-Events.
5. Tests decken Happy Path und mindestens zwei Fehlerfälle ab.
6. Falls ein externer LLM-Endpoint konfiguriert ist, wird er im Belegupload-Flow über ein OpenAI-Responses-kompatibles Payload-Format aufgerufen, ohne den Upload bei LLM-Fehlern zu blockieren.

## Technische Hinweise
- Dateigrößenlimit und erlaubte MIME-Typen serverseitig validieren.
- Storage-Zugriff kapseln, damit später S3/Objekt-Storage möglich bleibt.
- Keine direkte Dateiablage aus Templates ohne Service-Schicht.
- Schnittstelle so designen, dass OpenAI-Responses-kompatible Request/Response-Strukturen für lokale oder fremde LLM-Backends genutzt werden können.

## Out of Scope
- OCR/automatische Belegerkennung
- Vollständige DMS-Funktionalität

## Definition of Done
- Upload + Verknüpfung über UI/API nutzbar
- Auditierbarkeit dokumentiert
- Tests grün
