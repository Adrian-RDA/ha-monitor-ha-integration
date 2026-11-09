# HA Monitor – Home-Assistant-Integration

![HA Monitor](custom_components/ha_monitor_agent/brand/icon.png)

Asynchroner, ausgehender Monitoring-Agent für den Laravel-Empfänger von HA Monitor. Domain: `ha_monitor_agent`, Version: `0.4.0` (Monitoring Snapshot v2 und System Health v1). Unterstützt Home Assistant **ab 2026.10.4**, Python **3.14.2+**. Nutzt `psutil>=7.2.2`; System Monitor und andere zusätzliche Integrationen werden nicht benötigt.

## Installation

1. Dieses Repository auf GitHub bereitstellen: <https://github.com/Adrian-RDA/ha-monitor-ha-integration>. Für HACS/Marketplace muss es öffentlich erreichbar sein; die lokale Implementierung veröffentlicht nichts.
2. In **Marketplace** (HA 2026.11+) bzw. **HACS → Benutzerdefinierte Repositories** die Repository-Adresse mit Typ **Integration** hinzufügen und **HA Monitor** installieren. Bei HACS anschließend Home Assistant neu starten.
3. In Laravel bei der gewünschten aktiven Instanz einen Kopplungscode erzeugen. Er ist höchstens zehn Minuten gültig. Schon das Erstellen widerruft ein vorhandenes Agent-Token.
4. **Einstellungen → Geräte & Dienste → Integration hinzufügen → HA Monitor** öffnen. HTTPS-Basisadresse des Laravel-Servers, etwa `https://monitor.example.org`, und Kopplungscode eingeben. Ein Pfadpräfix wie `https://example.org/monitor` ist möglich; keinen `/api/v1/agents`-Pfad anhängen.
5. In Home Assistant den Sensor **Übertragungsstatus** prüfen. In Laravel muss nach dem ersten bestätigten Heartbeat der Status online erscheinen.

Für einen lokalen Test ohne Veröffentlichung den Ordner `custom_components/ha_monitor_agent` nach `<HA-Konfiguration>/custom_components/ha_monitor_agent` kopieren, HA neu starten und ab Schritt 3 fortfahren. Kein YAML und kein HA-Administrator-Token notwendig.

## Betrieb und Einstellungen

- Ein Heartbeat beim Start, danach im vom Server gelieferten Intervall, normalerweise alle 60 Sekunden. Übertragen werden ausschließlich die im [verbindlichen Protokoll](docs/PROTOCOL.md) definierten Felder: Instanz-UUID, Event-UUID und Zeitpunkte sowie HA- und Integrationsversion.
- Drei lokale Diagnosesensoren: Übertragungsstatus, letzter bestätigter Heartbeat und ausstehende Heartbeats. Der lokale Uploadstatus beschreibt die letzte Übertragung; den zeitbasierten Online-/Stale-Status berechnet Laravel selbst.
- Unter **Konfigurieren** lässt sich die gesamte Übertragung pausieren; lokale Erfassung läuft weiter. Monitoring-Uploads sind separat aktivierbar und standardmäßig aus. Die Änderung lädt den Eintrag neu. Nach mehr als 180 Sekunden ohne neuen Heartbeat zeigt Laravel die Instanz als veraltet an.
- Unter **Neu konfigurieren** lassen sich Empfängeradresse und Token durch erneutes Pairing ersetzen. Die Instanz-UUID muss gleich bleiben, auch bei einem umgezogenen Empfänger. Eine andere Instanz benötigt einen eigenen Integrationseintrag. Ein eingegebener Code wird auch bei nachträglich erkannter UUID-Abweichung verbraucht.
- Bei HTTP 401 stoppt der Sender und Home Assistant fordert eine erneute Authentifizierung an. Einen neuen Laravel-Code für dieselbe Instanz eingeben. Es gibt im v1-Vertrag weder automatische Token-Rotation noch eine Widerrufs-API für den Agenten.
- Das Token liegt ausschließlich in den Konfigurationsdaten des HA-Eintrags. Home Assistant verschlüsselt diesen Speicher nicht automatisch; auch HA-Backups können es enthalten. Der Kopplungscode wird nicht gespeichert. Logs, Sensorattribute und herunterladbare Diagnosen enthalten keine Zugangsdaten.
- Beim Entfernen in HA zusätzlich in Laravel widerrufen, wenn die Zugangsdaten serverseitig ungültig werden sollen.

## Eigenständige Datenerfassung (Sprint 5)

Die lokale Erfassung läuft im eigenen 60-Sekunden-Takt, auch bei deaktivierter Übertragung, Netzwerkfehlern, einem fehlenden Monitoring-Endpunkt oder einem ungültigen Token. Die allgemeine HA-Einstellung zur automatischen Entitätsaktualisierung (`pref_disable_polling`) stoppt diesen Agent-Takt nicht. Deaktivieren/Entfernen der Integration beendet ihn; Reaktivieren startet einen frischen Collector und die regelmäßigen Uploads. Voraussetzung ist ein eingerichteter Integrationseintrag; Laravel wird für die Messung nicht abgefragt. Neue Beobachtungen erhalten eine UUID und einen UTC-Zeitpunkt mit Mikrosekunden. Nach Start oder Reaktivierung ist CPU zunächst unbekannt, bis zwei gültige Messungen vorliegen; spätestens mit der nächsten gültigen Erfassung nach etwa 60 Sekunden liegt der erste CPU-Wert vor. RAM und Disk benötigen keine Anlaufmessung. Inventar und Updates bleiben während des HA-Starts unbekannt.

| Wert | Quelle und Bedeutung |
| --- | --- |
| CPU (`host`) | Delta der OS-CPU-Zeiten über das Messintervall, über alle sichtbaren logischen CPUs normiert; Gastzeiten werden nicht doppelt gezählt, I/O-Wartezeit zählt als Leerlauf. Kein Load Average. |
| RAM (`host`) | `(total − available) / total × 100` aus `psutil.virtual_memory()`. |
| Disk (`data`) | `used / total × 100` des Dateisystems am HA-Konfigurationsverzeichnis. Keine Addition mehrerer Volumes und kein abweichender Nenner für reservierte Blöcke. |
| Entitäten / Geräte | Entity Registry einschließlich deaktivierter Einträge bzw. Device Registry; keine Zählung aller Zustände als Entitäten. |
| Automationen / Szenen | Aktuell vorhandene Zustände der jeweiligen Domain, einschließlich ausgeschalteter Automationen. |
| Integrationen | Eindeutige erfolgreich geladene Domains aus `hass.config.components`, einschließlich YAML; Plattformnamen und mehrere Config Entries zählen nicht mehrfach. |

**Messbereich:** `host` bezeichnet die von psutil sichtbare Betriebssystemumgebung. Auf HA OS ist dies normalerweise die Kernel-/procfs-Sicht des Hosts. Bei Container-/Core-Installationen ist es ebenfalls die OS-Sicht, gegebenenfalls eine vom Laufzeitsystem virtualisierte Sicht. Diese Werte sind **keine Container-/cgroup-Auslastung und keine HA-Prozessauslastung**. Ein Container-Limit wird nicht mit einem Host-Zähler kombiniert. Ein physischer Host außerhalb der sichtbaren Umgebung wird nicht abgeleitet. Disk bezeichnet ausschließlich das Dateisystem der HA-Daten, auch wenn es ein Bind Mount oder ein separates Volume ist. `home_assistant` wird in dieser Version nicht ausgegeben. Fehlerhafte, nicht endliche oder außerhalb 0–100 liegende Werte werden als fehlend gemeldet; Berechtigungsfehler betreffen nur die jeweilige Metrik.

Unter **Konfigurieren**:

- **Monitoring-Snapshots übertragen** aktivieren; Heartbeats und separat freigegebenes Health-Monitoring laufen unabhängig davon.
- CPU, RAM und Disk einzeln zur Übertragung auswählen. Nicht ausgewählte Werte werden im vollständigen Snapshot ausdrücklich `unsupported`/null; die lokale Erfassung bleibt vollständig. Inventar wird als die fünf dokumentierten Summen übertragen.
- **Supervisor Core-/OS-Updates erfassen** aktiviert standardmäßig die auf HA OS vorhandenen nativen Update-Quellen. Identifikation erfolgt über stabile Registry-IDs, auch bei umbenannten Entitäten. OS wird auf Installationen ohne OS-Quelle nicht erfunden. Außerhalb von Supervisor erfolgt keine eigenständige Internetabfrage nach Core-Versionen; dort fehlt ohne ausgewählte Quelle eine zuverlässige Update-Aussage.
- **Alle Update-Quellen lokal erfassen** ist standardmäßig an. Aktive und versteckte `update.*`-Quellen sowie deaktivierte Registry-Einträge werden ohne zusätzliche Abfragen gelesen. Die Integration aktiviert oder installiert keine Updates.
- **Alle gefundenen Update-Quellen übertragen** ist jetzt standardmäßig an, auch bei bestehenden Einträgen ohne gespeicherten Wert. Diese Option erzwingt die vollständige Quellensuche auch bei ausgeschalteter lokaler Suchoption. Ein explizit gespeichertes `export_all_updates: false` bleibt erhalten: Dann werden nur angeforderte Core/OS- und zusätzlich ausgewählte Quellen übertragen; der Scope lautet `selected`.
- Monitoring verwendet ausschließlich **Snapshot v2**. `/api/v2/agents/monitoring` überträgt jede Quelle mit Kategorie, Originalversionen, Verfügbarkeit, Installationsfähigkeit, Fortschritt und den freigegebenen Quellen-/Gerätemetadaten. Shelly-Buildkennungen mit `/`, Immich und Proxmox ohne Installationsfunktion sind unterstützt. Es erfolgt kein Versionsvergleich und keine Umformung von Versionskennungen. Namen und Versionen können private Angaben enthalten und verlassen HA innerhalb des gewählten Exportumfangs.
- `coverage: complete` bedeutet, dass alle erforderlichen Quellen im angegebenen Scope zuverlässig klassifiziert wurden. Bei `partial` bleiben gültige Beobachtungen erhalten, auch wenn eine andere Quelle fehlt, fehlerhaft, unbekannt oder nicht verfügbar ist. Die Zahl angebotener Updates ist dann eine **Untergrenze**. `unknown` und `unsupported` behaupten keine gemessene Null. Automatisch gefundene deaktivierte Quellen sind nicht erforderlich; explizit angeforderte deaktivierte Quellen verhindern Vollständigkeit. Versteckte aktive Quellen zählen mit.
- Sieben Kategorien bleiben erhalten: `core`, `os`, `supervisor`, `addon`, `hacs`, `firmware`, `other`. Core/OS stehen zuerst. Übersprungene aktuelle Versionen zählen als angeboten; veraltete Skip-Metadaten auf einer aktuellen Quelle erzeugen kein Update. `supports_install: false` ist eine Beschreibung und kein Filter.
- Reparaturen und bereinigte Diagnosen zeigen unvollständige Erfassung oder durch die Auswahl ausgelassene Updates. Ungültige optionale Texte werden als unbekannt dargestellt und durch feste Fehlercodes gemeldet; sie verwerfen keine gültigen Quellen. Es werden keine Rohmetadaten in Logs, Diagnosen oder Sensorattribute übernommen.

Die Monitoring-Protokollauswahl wurde entfernt. Eine gespeicherte Option `monitoring_schema_version` wird beim Laden bereinigt, auch wenn zuvor 1 gewählt war. Alle übrigen Optionen und Zugangsdaten bleiben erhalten; erneutes Pairing ist nicht erforderlich. Pairing und Heartbeats verwenden weiterhin ihre unveränderten v1-Endpunkte. Monitoring verwendet ausschließlich `/api/v2/agents/monitoring`.

Neun zusätzliche lokale Diagnosesensoren (CPU/RAM/Disk, fünf Summen, verfügbare Updates) sind zunächst deaktiviert. Unter dem HA-Monitor-Gerät lassen sie sich einzeln aktivieren. Sie verwenden denselben Datenstand wie der Export, ohne eigene Abfragen. Ressourcen zeigen den `scope`; alle Sensoren zeigen `collection_status`. Fehlende Werte werden als nicht verfügbar angezeigt, niemals als gemessene Null. Der Update-Sensor zählt bei vollständiger lokaler Abdeckung auch die angebotenen, aber nicht zur Übertragung gewählten Updates. Seine Zahl kann deshalb größer als die übertragene Sammlung sein. Ausnahmen beim Lesen einer einzelnen Update-Entität verwerfen die übrigen Beobachtungen nicht. Die Diagnose unterscheidet `known_available`, `export_item_count` (Items im aktuellen Payload) und `blocked_known_available` (bekannte angebotene Updates, die nicht im erzeugten Payload darstellbar sind). Seine bereinigten Zähler unterscheiden offene/übersprungene, deaktivierte, unbekannte und nicht installierbare Updates sowie Erfassungsprobleme; bei unvollständiger Erfassung ist der Sensor nicht verfügbar und die Integrationsdiagnose zeigt weiterhin die bekannte Untergrenze.

## Systemgesundheit (Sprint 6, ab 0.4.0)

Unter **Konfigurieren** lässt sich **Health-v1-Erfassung und -Übertragung** unabhängig von Monitoring Snapshot v2 aktivieren. Standardmäßig ist Health ausgeschaltet. Die freigegebenen Bereiche werden sofort und danach alle **fünf Minuten** als vollständiger Bericht an `/api/v1/agents/health` gesendet:

- **Reparaturen:** aktive offene und ignorierte Hinweise mit stabiler Quell-ID und ursprünglichem Schweregrad.
- **Integrationsprobleme:** native Fehler- und Übergangszustände einzelner Config Entries. Geladene und bewusst deaktivierte Einträge werden ausgeschlossen.
- **Nicht verfügbare Entitäten:** ausschließlich aktivierte Entitäten mit tatsächlichem Zustand `unavailable`. Standardmäßig gilt eine leere explizite Auswahl; alle Entitäten und lokale Gerätezuordnungen sind separat freizugeben. Eine Gerätezuordnung behauptet keinen Geräteausfall.
- **Backups:** Status sowie beobachteter Start und erfolgreicher Abschluss nativer Backup-Läufe. Keine Dateiscans oder zusätzlichen Sensoren. Nach Start/Reload und bei verpasstem Laufbeginn bleibt der Status ehrlich `unknown`, bis ein vollständiger Lauf beobachtet wurde. Historische automatische Backup-Zeitwerte beweisen keinen letzten manuellen Erfolg. Fehlende/ausgefallene APIs ergeben `unsupported`/`unavailable`.

Namen werden immer als null übertragen. Quell- und Entitäts-IDs können private Informationen enthalten; nur ausdrücklich freigegebene Bereiche und Entitäten wählen. Keine Logs, Zugangsdaten, HA-Attribute oder Backup-Inhalte verlassen HA. Es entstehen keine neuen lokalen Sensoren oder künstlichen Gesundheitswerte.

Der Bericht enthält höchstens **100 Reparaturen, 256 Config Entries, 1.000 Entitäten und 262.144 Bytes**. Überschreitungen ergeben einen deterministischen Teilbestand mit `coverage: partial` und bekannter Gesamtzahl bzw. null. Ein eigener begrenzter Retry-Puffer und Backoff lassen Heartbeats und Snapshot v2 unabhängig weiterlaufen. Details zu Quellen, Backup-Grenzen, Datenschutz und Tests: [Health-Erfassung](docs/HEALTH_COLLECTION.md).

## Verbindungsfehler und Grenzen

| Fehler | Verhalten |
| --- | --- |
| Timeout, Netzwerkfehler, 5xx | Exponentieller Backoff mit Jitter: Heartbeats 2,5–900 Sekunden, Monitoring mindestens 15 und höchstens 900 Sekunden ohne längeres `Retry-After`. |
| 429 | Gültiges `Retry-After` als Mindestwartezeit, unterstützt Sekunden und HTTP-Datum; ohne gültigen Header normaler Backoff. |
| 401 | Uploads stoppen, Reauthentifizierungsdialog in HA. |
| 403, 400/413/415/422, unerwarteter Status, ungültige Bestätigung | Betroffenen Uploader stoppen, Hinweis unter **Reparaturen**. Ursache korrigieren und neu laden bzw. neu konfigurieren. |
| Zertifikatsfehler oder HTTP-Redirect | Uploads stoppen. Zertifikat, Kette, Hostname und Ingress korrigieren. Keine Zertifikatsumgehung und keine Weitergabe von Zugangsdaten an Redirect-Ziele. |

Pairing wird wegen des einmal verwendbaren Codes niemals automatisch wiederholt. Geht eine Pairing-Antwort verloren, einen neuen Code erstellen. Bei `Retry-After` über 24 Stunden stoppt der Sender mit einem Reparaturhinweis, statt früher als angefordert zu senden.

Der Arbeitsspeicherpuffer enthält höchstens 100 Beobachtungen; bei Überlauf wird die älteste verworfen. Beobachtungen über 23 Stunden sowie nach einer rückwärts korrigierten Uhr in der Zukunft liegende Beobachtungen werden verworfen. Event-IDs und Beobachtungsdaten bleiben bei Wiederholungen stabil; nur `sent_at` wird aktualisiert. **Jeder Ingest-Request enthält genau einen Heartbeat im `events`-Array**, auch wenn beim Pairing `max_batch_events: 100` zurückkommt. Dieser Wert ist eine Obergrenze: `PROTOCOL.md` erlaubt 1–100 Events; der Sender bleibt zusätzlich mit dem ursprünglichen Empfänger für genau einen Heartbeat kompatibel. Es werden keine weiteren Eventtypen oder Payload-Felder ergänzt. Die tatsächliche unkomprimierte Bytezahl berücksichtigt die beim Pairing gemeldete Grenze; ist schon ein einzelner Heartbeat zu groß, stoppt der Sender.

Nach Verbindungsausfällen werden wartende Heartbeats einzeln in Beobachtungsreihenfolge bestätigt und aktuelle Beobachtungen ergänzt. Der Rückstau wird mit höchstens 20 Requests pro Minute abgebaut. Laravel kann veraltet anzeigen, bis ein aktueller Heartbeat bestätigt wurde; alte Beobachtungen frischen den Status nicht auf. Bestätigt wird nur eine vollständige, plausibel quittierte Batch. Die Laravel-Deduplizierung schützt bei verlorenen Antworten vor erneutem Speichern. **Kein Exactly-once-Versprechen und kein persistenter Wiederholungspuffer:** Neustart, Neuladen und Optionsänderung verwerfen unbestätigte Daten; danach entsteht ein frischer Heartbeat. Einzelne Entitätszustände und beliebige Attribute werden nicht exportiert. Freigegebene Monitoring-Snapshots verwenden ausschließlich den separaten v2-Endpunkt.

Monitoring hält höchstens zwei eingefrorene Snapshots im Arbeitsspeicher: eine ausstehende und die neueste zusammengefasste Beobachtung. JSON-Serialisierung läuft im HA-Executor. Bei verlorener Bestätigung bleiben UUID, Beobachtungszeit und Inhalt identisch; nur `sent_at` wird erneuert. Neue Messungen ersetzen ausschließlich den neuesten wartenden Snapshot. Nach Wiederherstellung folgt dieser auf die bestätigte ausstehende Beobachtung.

V2 sendet **einen atomaren Snapshot mit 0–4.096 Quellen und höchstens 2.097.152 tatsächlich serialisierten, unkomprimierten Bytes einschließlich Envelope**. Sammlungen über 100 Einträge oder 64 KiB bleiben ein Request. Es gibt keine Chunks oder unabhängig ersetzenden Fragmente. Bei Erreichen der Erfassungsgrenze werden die erfassten Quellen ausdrücklich als `partial` gemeldet. Überschreitet dagegen der serialisierte Request die Bytegrenze, stoppt der Uploader mit `payload_too_large` und einem Reparaturhinweis; der letzte bestätigte Serverstand bleibt erhalten. Auswahl bzw. Metadatenmenge korrigieren und neu laden. Der Reverse Proxy muss das dokumentierte v2-Limit zulassen.

Es gibt höchstens einen Monitoring-Request gleichzeitig und maximal vier Versuche pro Minute. V2 hat eine separate Quote von sechs Requests/Minute pro Instanz und 30/Minute pro Quell-IP; die Heartbeat-Quote wird dadurch nicht belastet. Ein gültiges `Retry-After` bleibt eine Mindestwartezeit.

HTTP 404 am Monitoring-Endpunkt stoppt nur diesen Uploader bis zum Neuladen nach einem kompatiblen Backend-Update. HTTP 409 stoppt ihn wegen widersprüchlicher Wiederverwendung einer Snapshot-ID. Ein Monitoring-Fehler pausiert weder Heartbeats noch lokale Messungen. HTTP 401 auf einem der Endpunkte sperrt beide Uploadpfade und startet Reauthentifizierung; der lokale Datenstand wird weiterhin aktualisiert. Die bereinigte Integrationsdiagnose zeigt Uploadstatus, letzten Erfassungszeitpunkt sowie Erfassungsstatus und Exportauswahl für CPU/RAM/Disk. Damit lassen sich fehlende Erfassungen von blockierten Uploads unterscheiden. Monitoring beeinflusst keine Heartbeat-Zeitpunkte und keine Liveness-Aussage.

Bei HTTP 422 werden höchstens acht bekannte Schema-Feldpfade aus der begrenzten Fehlerantwort in Reparaturen und `monitoring.validation_fields` übernommen, etwa `snapshots.0.updates.items.14.disabled_by`. Antworttexte, abgelehnte Werte und beliebige Fehlerschlüssel bleiben ausgeschlossen. Native HA-Deaktivierungsgründe `hass`, `device` und `config_entry` werden gemäß v2 als `disabled` mit unbekanntem Grund (`disabled_by: null`) übertragen; `user` und `integration` bleiben erhalten. Eine solche Quelle verwirft keine gültigen Messwerte oder Angebote. Wenn die Oberfläche nach einem Update noch „Snapshot v1“ anzeigt, Browseransicht vollständig neu laden; die tatsächliche Schemaversion steht in der Diagnose.

## Entwicklung und Tests

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install --group dev
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/python scripts/validate_repository.py
```

Die Tests verwenden echte Home-Assistant-Config-Entries, Flows und Sensorplattformen sowie einen simulierten Laravel-Empfänger über HTTPS mit eigener, ausschließlich in Tests vertrauter CA. OS-Aufrufe und Supervisor-Antworten werden simuliert; getestet werden auch Scope/Nenner, Startup, fehlende Berechtigungen, Registry-Zählregeln, alle vier gemeldeten Updates, sieben Kategorien, vollständige/teilweise Erfassung, 4.096 Quellen, Requests über 64 KiB und das 2-MiB-Limit, feste Retry-Inhalte, 404/409, Coalescing, Uhrkorrektur und parallele Heartbeats bei wartender Erfassung bzw. wartendem Monitoring-HTTP. Sie benötigen keinen echten Laravel-Server. Die offizielle Testbibliothek `pytest-homeassistant` ist auf HA 2026.11.0 festgelegt. CI führt Tests, Ruff, Paketprüfungen sowie die offiziellen hassfest- und HACS-Actions aus.

Optionaler Gegencheck gegen den **echten Laravel-Validator** des benachbarten Checkouts (Sail muss bereits laufen):

```bash
HA_MONITOR_LARAVEL_PATH=/home/adrian/ha-monitor .venv/bin/pytest tests/test_laravel_contract.py
```

Dieser Test bootet Laravel über Sail und validiert nur die erzeugten Payloads mit `MonitoringSnapshotV2Request` und `HealthReportRequest`; er ruft keinen Controller auf, verwendet keine echten Zugangsdaten und schreibt keine Monitoringdaten. Geprüft werden alle nativen Deaktivierungsgründe, der Startup-Snapshot und eine teilweise Sammlung mit 161 Quellen. Health prüft zusätzlich vollständige/ausgewählte/Startup-Berichte, Größenüberschreitungen und laufende/erfolgreiche/fehlgeschlagene Backups. Ohne die Umgebungsvariable werden diese optionalen Tests übersprungen.

Offizielles hassfest lokal mit Docker:

```bash
docker run --rm -v "$PWD":/github/workspace ghcr.io/home-assistant/hassfest
```

Für die HACS-Action muss das GitHub-Repository veröffentlicht und seine Beschreibung, Issues und Topics eingerichtet sein. Repository-Dateien und CI sind vorbereitet; eine Katalogaufnahme ist ein separater Schritt. Vor Releases müssen beide offiziellen Actions erfolgreich sein. Tag und Manifestversion müssen übereinstimmen.

Manueller Abnahmetest mit einem regulär gültigen HTTPS-Empfänger:

1. Koppeln; Laravel-Versionen und Heartbeat-Zeitpunkte prüfen. Nach etwa 60 Sekunden muss ein weiterer Heartbeat erscheinen.
2. Empfänger kurz abschalten: HA bleibt bedienbar und zeigt Wiederholungsversuche. Nach Wiederherstellung muss der Puffer abgebaut werden.
3. Token in Laravel widerrufen: keine weiteren Uploadversuche; in HA mit einem neuen Code erneut authentifizieren.
4. Übertragung pausieren und fortsetzen. HA neu starten und Integration neu laden; jeweils frische Heartbeats ohne erneute Codeeingabe prüfen.
5. Monitoring aktivieren, Ressourcen und Update-Quellen wählen. Aktuelle Werte, Scopes sowie Core-/OS-Priorität im Backend prüfen. Lokale Sensoren bei Bedarf aktivieren und mit derselben Beobachtung vergleichen.
6. Core, Shelly, Immich und Proxmox: alle vier angebotenen Updates einschließlich Rohversionen und Installationsfähigkeit in Laravel prüfen. Eine zusätzlich ausgewählte Quelle deaktivieren: v2 muss `partial` und weiterhin die vier gültigen Angebote zeigen. Teil-Anzahlen als Untergrenze prüfen. Container-/Core-Installationen auf ihre tatsächlich sichtbaren Messbereiche prüfen.
7. Integration deaktivieren/entfernen: keine weiteren Uploads oder Erfassungsaufgaben. Serverseitigen Widerruf bei Bedarf separat ausführen.

## Architektur

`collectors.py` erfasst Ressourcen und Inventarsummen; `updates.py` hält das lokale Update-Inventar und die v2-Projektion. Datei-/OS-Zugriffe laufen ausschließlich über `hass.async_add_executor_job`. `monitoring.py` hält einen gemeinsamen `DataUpdateCoordinator` als Datenstand für Sensoren; ein eigener HA-Intervalltimer startet alle 60 Sekunden höchstens eine Erfassung, unabhängig von aktiviertem Entitätspolling oder lokalen Sensoren; einzelne Collector-Fehler ersetzen Werte durch Verfügbarkeitszustände. Keine per-Entity-Taskflut, keine Backend-Aufrufe beim Sammeln.

`health_collectors.py` erfasst die vier Health-Bereiche ohne Transport; `health.py` hält einen unabhängigen Fünf-Minuten-Timer und Health-v1-Uploader mit separatem Zustand.

`sender.py` bleibt der eigenständige Heartbeat-Worker im Runtime-Objekt `entry.runtime_data`. Daran hängt der lokale Monitoring-Coordinator mit einem separaten Uploader. Die Aufgaben warten nicht aufeinander. `client.py` teilt verifiziertes HTTPS, Authentifizierung und Bestätigungsvalidierung zwischen beiden dokumentierten Endpunkten. Plattformen lesen den gemeinsamen Datenstand. Unload beendet Timer, Listener und Entry-gebundene Hintergrundaufgaben; ein bereits im Betriebssystem laufender Executor-Aufruf kann durch Python nicht zwangsweise beendet werden.

Die Implementierung nutzt öffentliche HA-APIs und psutil, orientiert an [System Monitor](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/systemmonitor/coordinator.py), [Supervisor-Update-Entitäten](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/hassio/update.py), [HA Async-Richtlinien](https://contributors.home-assistant.io/core/internals/asyncio/) und [DataUpdateCoordinator](https://contributors.home-assistant.io/core/integrations/fetching-data/). Es wurde kein HA-Quellcode übernommen. Die verwendete Bibliothek [psutil](https://github.com/giampaolo/psutil/blob/master/LICENSE) steht unter BSD-3-Clause; sie wird als Abhängigkeit eingebunden, nicht vendort.

Weitere Grundlagen: [HA Config Entries](https://contributors.home-assistant.io/core/integrations/config-entries/), [Reauthentifizierung](https://contributors.home-assistant.io/core/integrations/reauth-reconfigure/), [offizielle Validierung](https://contributors.home-assistant.io/marketplace/reference/validation/).
