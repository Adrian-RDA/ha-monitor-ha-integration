# Update-Erfassung und Monitoring Snapshot v2

Stand: Integration **0.3.2**, Laravel-Vertrag aus [PROTOCOL.md](PROTOCOL.md), Abschnitt Monitoring Snapshot v2. Der v2-Empfänger ist bereits implementiert; diese Integration liefert jetzt den Sender. Pairing und Heartbeats bleiben v1. Der autoritative Protokolltext wurde für diese Implementierung nicht verändert.

## Vier gemeldete verfügbare Updates

Der Regressionstest `test_default_upgrade_sends_all_four_raw_versions_and_metadata` verwendet echte HA-Registries, die gemeldeten Entity-IDs und einen simulierten HTTPS-Empfänger. Ein bestehender Eintrag ohne neue Optionen sendet standardmäßig v2 und alle entdeckten Quellen, sobald Monitoring aktiviert ist.

| Quelle | Kategorie | Beispielversion | INSTALL |
| --- | --- | --- | --- |
| Home Assistant Core | `core` | `2026.11.1` | vorhanden |
| Shelly Carport-Licht | `firmware` | `20261101-160000/v1.15.0-gabcdef0` | vorhanden |
| Immich | `other` | `v2.3.1` | fehlt; wird trotzdem übertragen |
| Proxmox | `other` | `8.4.1-p0-d17` | fehlt; wird trotzdem übertragen |

Die OS-Quelle wird ebenfalls übertragen, auch wenn sie bereits aktuell ist. Die Sammlung enthält deshalb fünf Quellen und **vier verfügbare Updates**. Versionskennungen einschließlich Leerzeichen, Präfixen, Slash, Doppelpunkt und Unicode bleiben unverändert. Der Agent leitet Verfügbarkeit aus HA ab und vergleicht keine Versionen. Außerdem bleiben `supervisor`, `addon` und `hacs` als eigene Kategorien erhalten.

## Umfang, Vollständigkeit und Metadaten

- `export_all_updates` ist standardmäßig true; ein explizit gespeichertes false bleibt erhalten. `discover_updates: false` verhindert die für Export-all erforderliche Suche nicht. Die Übertragung selbst bleibt über `monitoring_enabled` separat aktivierbar und zunächst aus.
- `scope: all` erfasst aktive, versteckte und deaktivierte bekannte Quellen. Bei `selected` werden ausschließlich gewählte Quellen und angeforderte Core/OS-Quellen erfasst; alle Datensätze sind erforderlich.
- Automatisch gefundene deaktivierte Quellen sind nicht erforderlich. Ausgewählte deaktivierte Quellen, fehlende Quellen, unbekannte/nicht verfügbare Zustände, Erfassungsfehler oder Überschreiten der Erfassungsgrenze führen zu `partial`. Gültige Angebote anderer Quellen werden weiter übertragen. Teil-Anzahlen sind Untergrenzen.
- Keine zuverlässige Erfassung ergibt `unknown`, eine insgesamt nicht gewählte Erfassung `unsupported`. Deren leere Listen und Nullzähler behaupten keine aktuelle Installation. Eine zuverlässig leere Suche im Scope all ergibt `complete`.
- Nur aktuell übersprungene Versionen bleiben angebotene Updates. `available + skipped` ist die Zahl bekannter Angebote; alte Skip-Metadaten bei `up_to_date` zählen nicht mit.
- Der Payload enthält pro Quelle ausschließlich Schlüssel, Name, Kategorie, Verfügbarkeit, Integration, freigegebene Gerätefelder, Installationsfähigkeit, Originalversionen, Skip, Fortschritt, Deaktivierungsgrund und Erforderlichkeit. Geräte-IDs stammen aus der lokalen Device Registry. Hardwarekennungen, Seriennummern, IP/MAC, Release-URLs/-Texte und beliebige Attribute werden nicht exportiert.
- Ungültige optionale Texte werden ohne Umschreiben oder Abschneiden durch null ersetzt und mit festen lokalen Fehlercodes angezeigt. Die gültigen Quellen bleiben erhalten. Rohtexte erscheinen nicht in Logs, Diagnosen oder Sensorattributen.

## Große Sammlungen und Wiederholungen

V2 verwendet **einen atomaren Request** mit genau einem Snapshot, höchstens 4.096 Quellen und 2.097.152 unkomprimierten serialisierten Bytes einschließlich aller Metadaten und Envelope. Es gibt keine Chunk-Staging-API. Tests übertragen 101 und 4.096 verfügbare Updates, auch mit mehr als 64 KiB, über dieselbe Route. Verlorene Bestätigungen wiederholen unveränderte Snapshot-UUID, Beobachtungszeit und Inhalt. Die simulierte Datenbank quittiert den Retry als Duplikat; neue Beobachtungen erhalten neue UUIDs.

Die Erfassung hält bei mehr als 4.096 Quellen eine ausdrücklich teilweise Sammlung. Eine Überschreitung der **Transport-Bytegrenze** stoppt dagegen den Upload mit Reparaturhinweis und erhält den vorherigen Serverstand. Es werden keine unabhängig ersetzenden Fragmente und keine leere Ersatzliste gesendet. Neue Messungen laufen lokal weiter. Monitoring hält maximal zwei eingefrorene Beobachtungen; JSON-Serialisierung findet im HA-Executor statt. Ein Request gleichzeitig und mindestens 15 Sekunden zwischen Versuchen begrenzen die Rate. Retry-After, Backoff und Ablauf der Beobachtungen werden geprüft.

## Betrieb und bestehende Einträge

Monitoring verwendet ausschließlich Snapshot v2. Die Protokollauswahl ist entfernt; gespeicherte `monitoring_schema_version`-Werte werden beim Laden entfernt. Alle anderen Optionen, Pairing und Zugangsdaten bleiben erhalten. Pairing und Heartbeats bleiben auf ihren dokumentierten v1-Endpunkten. Ein eigener 60-Sekunden-Timer erhält die Erfassung unabhängig von Home Assistants Entitätspolling aufrecht. Nach Reaktivierung ist CPU in der ersten Messung unbekannt und wird ab der zweiten gültigen Messung übertragen. RAM/Disk stehen bereits bei der ersten gültigen Messung zur Verfügung. Deaktivieren/Entfernen beendet Timer und Erfassungsaufgabe.

Produktiver Abnahmetest: Integration auf dem HA-Testsystem aktualisieren und neu laden, Monitoring einschalten, Export-all und Ressourcen-Auswahl prüfen. In Laravel vier Angebote, die unveränderten Rohversionen und die beiden nicht direkt installierbaren Quellen kontrollieren. Explizit zuvor gespeicherte Export-Einschränkungen müssen bewusst erweitert werden. Danach eine zusätzliche ausgewählte Quelle vorübergehend deaktivieren: Laravel soll `partial` und die übrigen vier Angebote anzeigen. Skip/Unskip/Installation, Wiederholung nach Verbindungsunterbrechung und unabhängige Heartbeats prüfen. Ein HTTPS-Reverse-Proxy muss die dokumentierte v2-Bodygröße erlauben; die Integration ändert keine Infrastruktur.

Automatisierte Tests ersetzen keinen Hardware-/Live-Laravel-Abnahmetest. Es wurde ausschließlich in diesem Repository gearbeitet; kein Commit und keine Veröffentlichung erfolgen automatisch.

## Offizielle Grundlagen

- [HA Update-Entität und Features](https://contributors.home-assistant.io/core/entities/update/)
- [HA State-/Skip-/Fortschrittssemantik](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/update/entity.py)
- [Immich ohne Installationsfunktion](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/immich/update.py)
- [Proxmox-Update-Entität](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/proxmoxve/update.py)
- [Shelly-Firmwarekennungen](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/shelly/update.py)

## HTTP-422-Abgleich mit Laravel (0.3.2)

Die Live-Diagnose zeigte bei Agent 0.3.1 laufende CPU/RAM/Disk-Erfassung und aktivierten Export, aber einen mit HTTP 422 blockierten v2-Uploader. Die gemeldete Sammlung umfasste 161 Quellen, davon 20 deaktivierte und zehn nicht verfügbare Quellen. Die v1-Beschriftung der alten Reparaturmeldung ist kein Nachweis für einen v1-Request; die Diagnose bestätigt Schemaversion 2.

Beim Abgleich mit `/home/adrian/ha-monitor/app/Http/Requests/Api/MonitoringSnapshotV2Request.php` wurde ein tatsächlicher Vertragsbruch reproduziert: HA liefert weitere native Deaktivierungsgründe. Ein `hass`-Datensatz führte im echten Validator zu `snapshots.0.updates.items.0.disabled_by`. Der Sender bildet `hass`, `device` und `config_entry` jetzt auf den dokumentierten unbekannten Grund null ab, ohne die deaktivierte Quelle als aktiv zu behandeln oder alte Versionswerte wiederzuverwenden. Erforderliche deaktivierte Quellen bleiben teilweise; automatisch entdeckte ausgeschlossene Quellen verhindern Vollständigkeit nicht.

Der optionale Gegencheck in `tests/test_laravel_contract.py` läuft per Sail gegen die unveränderten Laravel-Validatoren. Er testet alle fünf nativen Gründe sowie Startup und eine teilweise 161-Quellen-Sammlung. Der normale HTTPS-Regressionstest prüft zusätzlich erforderliche/nicht erforderliche Quellen und die sichere Diagnose: höchstens acht bekannte Feldpfade aus begrenzten 422-Antworten, keine Antworttexte oder abgelehnten Werte. Ein permanenter Fehler bleibt gestoppt; es gibt keine unveränderten Wiederholungen oder v1-Ausweichübertragung. Der Laravel-Checkout wird nicht verändert.
