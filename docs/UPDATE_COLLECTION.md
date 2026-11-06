# Update-Erfassung und Übergabe an Laravel

Stand: Agent **0.2.1**. Der produktive Vertrag bleibt unverändert [Monitoring Snapshot v1](PROTOCOL.md#monitoring-snapshot-v1-sprint-5). Die unten beschriebene Laravel-Erweiterung ist ein **Arbeitsauftrag**, keine implementierte oder vom Agenten verwendete API.

## Befund und Prüfumfang

Im bisherigen Collector gab es keinen Filter auf `UpdateEntityFeature.INSTALL`. Die Unterscheidung wird jetzt ausdrücklich durch Tests mit echten HA-`UpdateEntity`-Instanzen abgesichert: `state = on` bedeutet verfügbar, auch bei `supported_features = 0`. Das INSTALL-Bit beschreibt lediglich die angebotene Installationsfunktion; Berechtigungen, Erreichbarkeit und ein erfolgreicher Installationsversuch folgen daraus nicht.

Drei reproduzierbare Lücken wurden im Agenten gefunden:

1. Außer Core/OS wurden nur manuell ausgewählte Entitäten überwacht. Nicht ausgewählte Shelly-, HACS-, App- oder andere Updates fehlten daher unabhängig von ihrer Installierbarkeit.
2. Die Versionsprüfung lief auch für `off`-Entitäten, die gar kein Update anzubieten hatten. Eine aktuelle Shelly-Entität mit einer Rohkennung wie `20241011-123000/v1.14.1-g1234567` konnte deshalb die gesamte Liste sperren. Jetzt werden nur tatsächlich angebotene Records auf v1-Darstellbarkeit geprüft.
3. Unvollständige Erfassung und v1-Kompatibilitätsfehler waren lokal kaum unterscheidbar. Das vollständige lokale Inventar sowie strukturierte, bereinigte Fehlerzähler bleiben jetzt erhalten.

Eine angebotene Firmware mit `/` in der Versionskennung ist weiterhin **nicht verlustfrei durch v1 übertragbar**. V1 erlaubt dort nur `[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}`. Ein einziges nicht darstellbares angebotenes Update, eine fehlende überwachte Quelle oder mehr als 100 angebotene Updates erfordert laut Vertrag `unavailable` mit leerer Liste. Die lokale Erfassung erkennt die bekannten Updates trotzdem. Versionen werden weder gekürzt noch in vermeintliche Semver-Versionen umgeschrieben. Bekannte installierte Rohversionen werden nicht zu null umgedeutet, um die Validierung zu umgehen.

Der komplette HA-Pfad einschließlich TLS-Payload, Bestätigung und simulierter Speicherung wurde getestet. Laravel-Quellcode, reale Datenbank und produktive Darstellung liegen außerhalb dieses Repositorys und wurden **nicht** gelesen oder verändert. Die folgenden Laravel-Prüfungen sind deshalb konkret aus dem bestehenden Vertrag abgeleitete Aufgaben, keine behaupteten Befunde im Laravel-Code.

## Aktueller Datenweg

1. `updates.collect_update_inventory()` liest einmal je Erfassungszyklus die HA-Zustände und Registries, ohne Netzwerkabfragen oder Installationsaktionen.
2. Automatische Erfassung berücksichtigt aktive und versteckte `update.*`-Entitäten einschließlich nicht installierbarer Updates, ergänzt um Registry-Einträge zur Erkennung von deaktivierten oder fehlenden Quellen. Die Option ist standardmäßig an; manuelle Auswahl bleibt als zusätzliche verbindliche Quellenliste erhalten. Core/OS können separat ausgenommen werden. Höchstens 4.096 Quellen werden lokal verarbeitet; Überschreitung meldet `source_limit` statt eines vollständigen Teilergebnisses.
3. `MonitoringCoordinator.update_inventory` hält unveränderliche Beobachtungen gemeinsam mit dem zugehörigen Snapshot. Pro Quelle bleiben ausschließlich ausgewählte Felder bis jeweils 256 Zeichen erhalten: Entity-ID, Name, Kategorie, Integration (`source`), lokale Device-ID, Gerätename/Hersteller/Modell, installierte/neue/übersprungene Version, `supports_install`, Installationsfortschritt, deaktiviert-durch und Verfügbarkeitszustand. Unlesbare/zu große Versionsfelder werden als nicht darstellbar gemeldet. Release-URLs, Release Notes, Config-Entry-Daten und Gerätekennungen wie MAC/IP werden nicht übernommen.
4. `UpdateInventory.as_protocol()` erstellt ausschließlich die fünf erlaubten v1-Itemfelder. Kategorien Core/OS bleiben priorisiert; Supervisor, Apps/Add-ons, HACS, Firmware und sonstige Updates werden als `other` übertragen. Quell-/Gerätemetadaten und Installierbarkeit bleiben bis zur Vertragserweiterung lokal im Runtime-Inventar, nicht im HTTP-Payload.
5. `MonitoringUploader` friert diesen Snapshot mit UUID und Beobachtungszeit ein. Wiederholungen behalten Inhalt, ID und Zeitpunkt. HTTPS bleibt verifiziert, Redirects bleiben gesperrt, Heartbeats bleiben unabhängig.
6. Der dokumentierte Laravel-Empfänger validiert den kompletten Snapshot und aktualisiert `monitoring_currents`, `monitoring_samples`, `monitoring_state_changes` und `monitoring_receipts`. Die Frontend-Projektion darf Verfügbarkeit nicht nach Installierbarkeit filtern und `unavailable` niemals als null verfügbare Updates darstellen.

## Fachliche Zustände

| HA-Quelle | Lokales Ergebnis | Behandlung |
| --- | --- | --- |
| `on`, INSTALL gesetzt | `available`, `supports_install: true` | Verfügbar; wird bei v1-kompatiblen Metadaten übertragen. |
| `on`, INSTALL nicht gesetzt | `available`, `supports_install: false` | Ebenfalls verfügbar; externe/manuelle Installation darf die Anzeige nicht unterdrücken. Andere Feature-Bits ohne INSTALL ändern das nicht. |
| Features fehlen/ungültig | `supports_install: null` | Fähigkeit unbekannt; kein Filter auf Verfügbarkeit. |
| `off`, `skipped_version == latest_version`, installiert ungleich neu | `skipped` | Weiterhin angeboten, aber ausdrücklich übersprungen; v1 kann dieses Kennzeichen nicht übertragen. |
| `off`, keine aktuell übersprungene Version | `up_to_date` | HA-Aussage übernehmen; keine eigene Versionssortierung. Veraltete Skip-Metadaten allein erzeugen kein Update. |
| Registry-Eintrag deaktiviert | `disabled` | Nicht einschalten, keinen veralteten `on`-Zustand verwenden. Automatisch gefundene deaktivierte Quellen sind aus der aktiven Abdeckung ausgenommen. Explizit ausgewählte bzw. angeforderte Core/OS-Quellen machen die Abdeckung unvollständig. |
| Zustand fehlt/`unavailable` | `unavailable` | Keine erfundene Update-Aussage, auch wenn alte Versionen bekannt sind. Bekannte Updates anderer Quellen bleiben lokal erhalten; v1 darf keine Teilmenge übertragen. |
| `unknown`/anderer Zustand | `unknown` | Abdeckung unvollständig. |

Deaktivierte Entitäten liefern keine verlässliche neue Versionsbeobachtung. Insbesondere sind manche Shelly-Update-Entitäten laut offizieller HA-Integration standardmäßig deaktiviert. Ohne sie durch den Betreiber zu aktivieren oder eine separat vereinbarte native Datenquelle einzuführen, kann der Agent keine verfügbaren Updates dieser Geräte behaupten. Ebenso kann er keine Updates finden, die HA überhaupt nicht als Quelle bereitstellt. Versteckte, aber aktive Entitäten werden dagegen berücksichtigt.

Der optionale lokale Sensor **Verfügbare Updates** zählt bekannte angebotene Updates einschließlich aktuell übersprungener Versionen, solange die lokale Abdeckung vollständig ist. Seine Attribute bzw. die Integrationsdiagnose enthalten nur Zähler und feste Fehlercodes: `pending`, `skipped`, `disabled`, `unavailable`, `not_installable`, `installability_unknown`, `known_available`, `protocol_status`, `protocol_issues`. Bei unvollständiger Abdeckung wird der Sensor nicht verfügbar; `known_available` zeigt in der Integrationsdiagnose weiterhin die bekannte Untergrenze. Home Assistant blendet zusätzliche Sensorattribute im nicht verfügbaren Zustand aus. Ein lokaler Wert kann korrekt sein, während `protocol_status` wegen eines v1-Limits nicht verfügbar ist. Keine Rohmetadaten werden in Logs, Diagnosen oder Entity-Attributen ausgegeben.

## Konkrete Aufgaben im Laravel-Repository

### Sofort gegen das bestehende v1 prüfen

- Einen akzeptierten Snapshot mit mindestens zwei `other`-Items kontrollieren, die von HA ohne INSTALL-Fähigkeit als `on` gemeldet werden. In v1 gibt es **gar kein** Installierbarkeitsfeld: Empfänger, aktuelle Projektion, ACL-geprüfte Page-Props und UI dürfen keine Installierbarkeit anhand von Name, Integration oder Produkttyp hineininterpretieren.
- Von Request-Validierung über Receipt/Transaktion bis zu `monitoring_currents` und Frontend alle Itemschlüssel vergleichen. Ungewöhnliche Produktnamen, Gerätefirmware und HACS dürfen nicht aus `other` herausgefiltert werden. Darstellungsgrenzen müssen kenntlich sein; eine gekürzte Liste ist kein vollständiger Gesamtbestand.
- `status != ok` als „Update-Situation unbekannt/nicht verfügbar“ anzeigen; eine leere Liste in diesem Fall ist keine Aussage „aktuell“. V1 ist eine vollständige Ersetzung und löscht vorherige bekannte Items bei einem neuen nicht verfügbaren Zustand. Alte Werte dürfen nur ausdrücklich als letzter bekannter Stand gezeigt werden.
- Reihenfolge Core, OS, weitere nach stabilem Schlüssel prüfen. Snapshot-Reihenfolge, Deduplizierung und Re-Pairing dürfen keine alte Version als neue Beobachtung erscheinen lassen.
- Mindestens eine reale fehlende Shelly-Quelle mit Agent-Diagnose abgleichen: entdeckt/aktiv, `on`, gültige installierte/neue v1-Version, vollständige Abdeckung, tatsächlich akzeptierte Snapshot-ID. Wenn der Collector bereits `unavailable` sendet, kann die UI keine verlorenen Einzelitems aus diesem Snapshot rekonstruieren.

### Erforderliche kompatibel versionierte Erweiterung

Das bestehende v1-Schema darf nicht stillschweigend erweitert werden. Für verlustfreie Firmware-/Metadatenübertragung ist ein eigener **Monitoring Snapshot v2** mit separatem Endpoint, zum Beispiel `POST /api/v2/agents/monitoring`, festzulegen und zuerst im Backend zu implementieren. Der aktuelle Agent sendet ausschließlich v1; es gibt keinen automatischen Versions-Fallback.

Der neue Vertrag benötigt mindestens:

| Feld | Festzulegende Semantik |
| --- | --- |
| `updates.coverage` | `complete`, `partial`, `unknown`, `unsupported`; partielle bekannte Items müssen erlaubt und als Teilmenge sichtbar sein. |
| Item `availability` | `available`, `skipped`, `up_to_date`, `disabled`, `unknown`, `unavailable`; nicht aus einem Versionsvergleich im Backend ableiten. |
| Item `supports_install` | Striktes Boolean oder null; nur die Installationsfunktion betrifft dies, niemals das Vorhandensein eines Updates. |
| Item `installed_version`, `available_version`, `skipped_version` | Originale Kennungen als begrenzte Unicode-Texte bis 256 Zeichen ohne Unicode-Control-/Formatzeichen, nullable nach klarer Bedeutung. `/` und andere übliche Herstellerformate zulassen; keine serverseitige Semver-Erzwingung. Eine `on`-Aussage mit unbekannter Versionskennung bleibt als verfügbare, unvollständig beschriebene Quelle sichtbar. |
| Item `source` | Integration-Domain oder null; keine Config-Entry-Daten oder Zugangsdaten. |
| Item `category` | `core`, `os`, `supervisor`, `addon`, `hacs`, `firmware`, `other`; keine Prioritätsentscheidung anhand eines frei wählbaren Namens. |
| Item `device` | Optionales Objekt mit instanzlokaler Device-ID, Name, Hersteller, Modell; keine MAC/IP/Seriennummern oder vollständigen Registry-Einträge. Zugriff und Ausgabe wie andere Instanzdaten per ACL schützen. |
| Item `in_progress`, `disabled_by` | Explizite Typen und Zustände, keine beliebigen Fehlertexte. |
| Abdeckungszähler | Aktive, deaktivierte, unbekannte Quellen und bekannte verfügbare/übersprungene Updates konsistent validieren; keine Behauptung eines exakten Gesamtwerts bei `partial`. |

Für größere Inventare muss außerdem **vor Implementierung** die Übertragung vollständiger Sammlungen oberhalb von 100 Items bzw. 65.536 Bytes versioniert vereinbart werden. Ein konkreter Ansatz ist eine begrenzte chunkweise v2-Sammlung mit `collection_id`, `observed_at`, `chunk_index`, `chunk_count` und unveränderlichen Chunk-IDs/Inhalten. Das Backend veröffentlicht sie erst nach vollständigem Eingang aller Teile atomar, begrenzt Anzahl/Bytes/TTL unvollständiger Sammlungen und dedupliziert identische Wiederholungen; widersprüchliche Inhalte derselben ID bleiben ein Konflikt. Ohne diese Erweiterung darf ein Agent ein großes Inventar nicht auf mehrere voneinander unabhängig ersetzende v1-Snapshots aufteilen.

Datenbank/Projektion erweitern: genannte Metadaten und Abdeckung erhalten; Normalisierung und Receipt-Hashes um die neuen Felder erweitern; Statusübergänge einschließlich übersprungen/erneut angeboten/aktuell/nicht verfügbar historisieren. Die API-Resource/Page-Props müssen die Felder durchreichen. Frontend: verfügbare Updates unabhängig von `supports_install` listen; „manuell/extern installieren“ separat kennzeichnen, übersprungene Updates kenntlich machen und bei Bedarf aufklappbar gruppieren; deaktivierte/unbekannte Quellen als Abdeckungslücke zeigen. Ein Installationsbutton darf nur aus einer separat autorisierten Funktion entstehen – HA Monitor führt derzeit keine Installationsaktionen aus.

### Gemeinsame Abnahmetests

1. Core + OS + Supervisor + App + HACS + mehrere Shelly-Firmwareupdates + zwei `on`-Updates ohne INSTALL werden erkannt. Im kompatiblen v1-Fall alle Schlüssel über TLS, Speicherung und UI vergleichen.
2. Reale Shelly-Rohversion mit `/`: lokal exakt erhalten; v1 ausdrücklich nicht verfügbar; nach v2-Einführung exakt speichern und escaped rendern.
3. `off`-Shelly ohne angebotenes Update mit derselben Rohversionskennung darf andere kompatible angebotene Updates nicht blockieren.
4. Aktuelle übersprungene Version, danach aufgehobener Skip, danach installiert: fachliche Übergänge und Zähler prüfen. Ein veralteter Skip darf kein neues Update erzeugen.
5. Deaktivierte/versteckte/fehlende/nicht verfügbare Quelle: keine automatische Aktivierung, keine alte `on`-Beobachtung als aktuell ausgeben, bekannte Teilmenge nicht als vollständig darstellen.
6. Mehr als 100 Quellen, aber nur ein verfügbares Update: v1 funktioniert. Mehr als 100 angebotene Updates: v1 ausdrücklich nicht verfügbar; v2 später vollständige atomare Zusammensetzung testen.
7. Verlorene Bestätigung, Retry, Re-Pairing, verspätete Snapshots, Quoten sowie ACLs und HTML-Escaping von Geräte-/Versionsnamen testen. Heartbeats dürfen durch Update-Probleme nicht beeinträchtigt werden.

## Offizielle Grundlagen

- [HA Update-Entität und Features](https://contributors.home-assistant.io/core/entities/update/)
- [HA State-/Skip-Semantik](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/update/entity.py)
- [Shelly-Update-Implementierung und Firmwarekennungen](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/shelly/update.py)

Es wurde kein HA-Quellcode übernommen. Die Änderungen bleiben in diesem Repository; Laravel und Infrastruktur wurden nicht verändert.
