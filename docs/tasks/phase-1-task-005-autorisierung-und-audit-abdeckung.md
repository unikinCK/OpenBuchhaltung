# Task P1-005: Autorisierung härten + Audit-Abdeckung erweitern

## Ziel
Die Kernprozesse gegen unautorisierte Nutzung absichern und Audit-Logs auf alle buchungsrelevanten Aktionen ausweiten.

## Kontext
Aktuell existiert ein Demo-Login. Für belastbaren MVP-Betrieb fehlen durchgängige Autorisierungsregeln.

## Abhängigkeit
- **Input aus:** vorhandenes Auth-Modul
- **Input aus:** Audit-Log-Service

## Scope
- Rollenbasierte Guards für API/UI-Kernendpunkte
- Klarer Berechtigungsschnitt (Admin/Buchhalter/Prüfer)
- Audit-Events für weitere relevante Aktionen (z. B. Kontoanlage, Import, Belegverknüpfung)
- Fehlermeldungen/HTTP-Codes für denied access vereinheitlichen
- Tests für erlaubte und verbotene Pfade

## Akzeptanzkriterien
1. Ungültige/fehlende Berechtigungen blockieren schreibende Aktionen.
2. Lesende Prüfer-Rolle erhält nur freigegebene Endpunkte.
3. Audit-Logs decken zentrale Änderungen vollständig ab.
4. Integrationstests prüfen mindestens je Rolle einen Allowed/Denied-Fall.

## Technische Hinweise
- AuthZ-Checks zentralisieren (Decorator/Policy-Layer).
- Keine Logikduplikate zwischen UI- und API-Endpunkten.

## Out of Scope
- Externe Identity-Provider (OIDC/SAML)
- Mandantenübergreifendes Support-Modell

## Definition of Done
- Rollenregeln technisch erzwungen
- Audit-Abdeckung dokumentiert
- Tests grün
