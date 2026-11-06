# HA Monitor – Home-Assistant-Integration

![HA Monitor](custom_components/ha_monitor_agent/brand/icon.png)

Asynchroner, ausgehender Monitoring-Agent für den Laravel-Empfänger von HA Monitor. Domain: `ha_monitor_agent`, Version: `0.2.1` (Sprint 5). Unterstützt Home Assistant **ab 2026.10.4**, Python **3.14.2+**. Nutzt `psutil>=7.2.2`; System Monitor und andere zusätzliche Integrationen werden nicht benötigt.

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

Die lokale Erfassung läuft alle 60 Sekunden, auch bei deaktivierter Übertragung, Netzwerkfehlern, einem fehlenden Monitoring-Endpunkt oder einem ungültigen Token. Voraussetzung ist ein eingerichteter Integrationseintrag; Laravel wird für die Messung nicht abgefragt. Neue Beobachtungen erhalten eine UUID und einen UTC-Zeitpunkt mit Mikrosekunden. Nach dem Start ist CPU zunächst unbekannt, bis zwei gültige Messungen vorliegen. Inventar und Updates bleiben während des HA-Starts unbekannt.

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

- **Monitoring-Snapshots übertragen** aktivieren; vorher werden nur Heartbeats übertragen.
- CPU, RAM und Disk einzeln zur Übertragung auswählen. Nicht ausgewählte Werte werden im vollständigen Snapshot ausdrücklich `unsupported`/null; die lokale Erfassung bleibt vollständig. Inventar wird als die fünf dokumentierten Summen übertragen.
- **Supervisor Core-/OS-Updates erfassen** aktiviert standardmäßig die auf HA OS vorhandenen nativen Update-Quellen. Identifikation erfolgt über stabile Registry-IDs, auch bei umbenannten Entitäten. OS wird auf Installationen ohne OS-Quelle nicht erfunden. Außerhalb von Supervisor erfolgt keine eigenständige Internetabfrage nach Core-Versionen; dort fehlt ohne ausgewählte Quelle eine zuverlässige Update-Aussage.
- **Alle Update-Entitäten automatisch erfassen** ist standardmäßig an und erfasst alle aktiven `update.*`-Quellen, auch versteckte Entitäten und Updates ohne Installationsfunktion: Core, OS, Supervisor, Apps/Add-ons, HACS, Firmware und weitere. Manuelle Auswahl überwacht zusätzliche Quellen verbindlich, auch wenn die automatische Erfassung ausgeschaltet wird. Deaktivierte Quellen werden erkannt, aber nicht eingeschaltet oder als aktuelle Messung ausgegeben. Lokal sind höchstens 4.096 Quellen erlaubt; v1 kann höchstens 100 tatsächlich angebotene Updates übertragen. Core und OS erscheinen zuerst. Übersprungene Versionen bleiben separat erkennbar und zählen weiterhin als angeboten.

`ok` mit leerer Update-Liste bedeutet: Alle konfigurierten Quellen wurden erfolgreich gelesen und melden kein verfügbares Update. Ohne nutzbare Quelle gilt `unsupported`. Fehlt eine aktiv überwachte Quelle, ist sie unbekannt/nicht verfügbar, ist eine ausdrücklich gewählte Quelle deaktiviert oder lässt sich eine angebotene Version nicht in v1 darstellen, ist die **gesamte übertragene** Sammlung `unavailable` mit leerer Liste. Eine bekannte Teilmenge wird nicht als vollständig ausgegeben. HA OS nutzt die vorhandenen Supervisor-APIs/-Registries; die Integration installiert weder HACS noch Geräteintegrationen. Bereits aktuelle Quellen (`off`) werden nicht wegen ungewöhnlicher Versionsformate blockiert. Bei noch angebotener Shelly-Firmware kann dagegen die v1-Versionssyntax, beispielsweise wegen `/`, eine verlustfreie Übertragung verhindern. Metadaten zu Quelle, Gerät, Originalversion und Installierbarkeit bleiben dann im lokalen Runtime-Inventar erhalten. Der produktive API-Vertrag wird nicht umgangen. [Analyse und konkrete Laravel-Aufgaben](docs/UPDATE_COLLECTION.md) beschreiben die nötige Erweiterung.

Neun zusätzliche lokale Diagnosesensoren (CPU/RAM/Disk, fünf Summen, verfügbare Updates) sind zunächst deaktiviert. Unter dem HA-Monitor-Gerät lassen sie sich einzeln aktivieren. Sie verwenden denselben Datenstand wie der Export, ohne eigene Abfragen. Ressourcen zeigen den `scope`; alle Sensoren zeigen `collection_status`. Fehlende Werte werden als nicht verfügbar angezeigt, niemals als gemessene Null. Der Update-Sensor zählt bei vollständiger lokaler Abdeckung auch die angebotenen, aber in v1 nicht darstellbaren Updates. Seine bereinigten Zähler unterscheiden offene/übersprungene, deaktivierte, unbekannte und nicht installierbare Updates sowie v1-Probleme; bei unvollständiger Erfassung ist der Sensor nicht verfügbar und die Integrationsdiagnose zeigt weiterhin die bekannte Untergrenze.

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

Nach Verbindungsausfällen werden wartende Heartbeats einzeln in Beobachtungsreihenfolge bestätigt und aktuelle Beobachtungen ergänzt. Der Rückstau wird mit höchstens 20 Requests pro Minute abgebaut. Laravel kann veraltet anzeigen, bis ein aktueller Heartbeat bestätigt wurde; alte Beobachtungen frischen den Status nicht auf. Bestätigt wird nur eine vollständige, plausibel quittierte Batch. Die Laravel-Deduplizierung schützt bei verlorenen Antworten vor erneutem Speichern. **Kein Exactly-once-Versprechen und kein persistenter Wiederholungspuffer:** Neustart, Neuladen und Optionsänderung verwerfen unbestätigte Daten; danach entsteht ein frischer Heartbeat. Einzelne Entitätszustände und beliebige Attribute werden nicht exportiert. Freigegebene Monitoring-Snapshots verwenden ausschließlich den separaten Endpunkt `/api/v1/agents/monitoring`.

Monitoring hält höchstens zwei vollständige Snapshots im Arbeitsspeicher: eine unveränderliche ausstehende Beobachtung und die neueste zusammengefasste Beobachtung. Bei Verlust einer Bestätigung bleiben UUID, Zeit und Inhalt identisch; neue Messungen ersetzen nur den neuesten wartenden Snapshot. Nach Wiederherstellung wird anschließend der aktuelle Datenstand übertragen. Es gibt genau einen Monitoring-Request gleichzeitig, mit genau einem vollständigen Snapshot und höchstens 65.536 unkomprimierten Bytes einschließlich Envelope. Bei einer zu großen Update-Sammlung wird sie ausdrücklich als `unavailable` gemeldet. Monitoring sendet höchstens vier Requests pro Minute; zusammen mit dem Heartbeat-Rückstau bleibt dies unter der gemeinsamen Instanzquote von 30 Requests/Minute. Ein gültiges `Retry-After` bleibt eine Mindestwartezeit.

HTTP 404 am Monitoring-Endpunkt stoppt nur diesen Uploader bis zum Neuladen nach einem kompatiblen Backend-Update. HTTP 409 stoppt ihn wegen widersprüchlicher Wiederverwendung einer Snapshot-ID. Ein Monitoring-Fehler pausiert weder Heartbeats noch lokale Messungen. HTTP 401 auf einem der Endpunkte sperrt beide Uploadpfade und startet Reauthentifizierung; der lokale Datenstand wird weiterhin aktualisiert. Die getrennten lokalen Monitoring-Uploaddaten sind in der bereinigten Integrationsdiagnose sichtbar. Monitoring beeinflusst keine Heartbeat-Zeitpunkte und keine Liveness-Aussage.

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

Die Tests verwenden echte Home-Assistant-Config-Entries, Flows und Sensorplattformen sowie einen simulierten Laravel-Empfänger über HTTPS mit eigener, ausschließlich in Tests vertrauter CA. OS-Aufrufe und Supervisor-Antworten werden simuliert; getestet werden auch Scope/Nenner, Startup, fehlende Berechtigungen, Registry-Zählregeln, Update-Vollständigkeit, feste Retry-Inhalte, 404/409, Coalescing, Uhrkorrektur und parallele Heartbeats bei wartender Erfassung bzw. wartendem Monitoring-HTTP. Sie benötigen keinen echten Laravel-Server. Die offizielle Testbibliothek `pytest-homeassistant` ist auf HA 2026.11.0 festgelegt. CI führt Tests, Ruff, Paketprüfungen sowie die offiziellen hassfest- und HACS-Actions aus.

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
6. Eine ausgewählte Update-Quelle deaktivieren: Update-Sammlung muss unbekannt/nicht verfügbar sein, statt null verfügbare Updates zu behaupten. Container-/Core-Installationen auf ihre tatsächlich sichtbaren Messbereiche prüfen.
7. Integration deaktivieren/entfernen: keine weiteren Uploads oder Erfassungsaufgaben. Serverseitigen Widerruf bei Bedarf separat ausführen.

## Architektur

`collectors.py` erfasst Ressourcen und Inventarsummen; `updates.py` hält das lokale Update-Inventar und die getrennte v1-Projektion. Datei-/OS-Zugriffe laufen ausschließlich über `hass.async_add_executor_job`. `monitoring.py` hält einen gemeinsamen, alle 60 Sekunden aktualisierten `DataUpdateCoordinator` unabhängig von aktivierten Sensoren; einzelne Collector-Fehler ersetzen Werte durch Verfügbarkeitszustände. Keine per-Entity-Taskflut, keine Backend-Aufrufe beim Sammeln.

`sender.py` bleibt der eigenständige Heartbeat-Worker im Runtime-Objekt `entry.runtime_data`. Daran hängt der lokale Monitoring-Coordinator mit einem separaten Uploader. Die Aufgaben warten nicht aufeinander. `client.py` teilt verifiziertes HTTPS, Authentifizierung und Bestätigungsvalidierung zwischen beiden dokumentierten Endpunkten. Plattformen lesen den gemeinsamen Datenstand. Unload beendet Timer, Listener und Entry-gebundene Hintergrundaufgaben; ein bereits im Betriebssystem laufender Executor-Aufruf kann durch Python nicht zwangsweise beendet werden.

Die Implementierung nutzt öffentliche HA-APIs und psutil, orientiert an [System Monitor](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/systemmonitor/coordinator.py), [Supervisor-Update-Entitäten](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/hassio/update.py), [HA Async-Richtlinien](https://contributors.home-assistant.io/core/internals/asyncio/) und [DataUpdateCoordinator](https://contributors.home-assistant.io/core/integrations/fetching-data/). Es wurde kein HA-Quellcode übernommen. Die verwendete Bibliothek [psutil](https://github.com/giampaolo/psutil/blob/master/LICENSE) steht unter BSD-3-Clause; sie wird als Abhängigkeit eingebunden, nicht vendort.

Weitere Grundlagen: [HA Config Entries](https://contributors.home-assistant.io/core/integrations/config-entries/), [Reauthentifizierung](https://contributors.home-assistant.io/core/integrations/reauth-reconfigure/), [offizielle Validierung](https://contributors.home-assistant.io/marketplace/reference/validation/).
