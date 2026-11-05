# HA Monitor – Home-Assistant-Integration

![HA Monitor](custom_components/ha_monitor_agent/brand/icon.png)

Asynchroner, ausgehender Heartbeat-Agent für den Laravel-Empfänger von HA Monitor. Domain: `ha_monitor_agent`, Version: `0.1.0`. Unterstützt Home Assistant **ab 2026.10.4**, Python **3.14.2+**. Keine zusätzlichen Laufzeit-Dependencies.

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
- Unter **Konfigurieren** lässt sich die Übertragung pausieren. Die Änderung lädt den Eintrag neu. Nach mehr als 180 Sekunden ohne neuen Heartbeat zeigt Laravel die Instanz als veraltet an.
- Unter **Neu konfigurieren** lassen sich Empfängeradresse und Token durch erneutes Pairing ersetzen. Die Instanz-UUID muss gleich bleiben, auch bei einem umgezogenen Empfänger. Eine andere Instanz benötigt einen eigenen Integrationseintrag. Ein eingegebener Code wird auch bei nachträglich erkannter UUID-Abweichung verbraucht.
- Bei HTTP 401 stoppt der Sender und Home Assistant fordert eine erneute Authentifizierung an. Einen neuen Laravel-Code für dieselbe Instanz eingeben. Es gibt im v1-Vertrag weder automatische Token-Rotation noch eine Widerrufs-API für den Agenten.
- Das Token liegt ausschließlich in den Konfigurationsdaten des HA-Eintrags. Home Assistant verschlüsselt diesen Speicher nicht automatisch; auch HA-Backups können es enthalten. Der Kopplungscode wird nicht gespeichert. Logs, Sensorattribute und herunterladbare Diagnosen enthalten keine Zugangsdaten.
- Beim Entfernen in HA zusätzlich in Laravel widerrufen, wenn die Zugangsdaten serverseitig ungültig werden sollen.

## Verbindungsfehler und Grenzen

| Fehler | Verhalten |
| --- | --- |
| Timeout, Netzwerkfehler, 5xx | Exponentieller Backoff mit Jitter, 2,5–900 Sekunden; nur die erste Ausfallmeldung und die Wiederherstellung werden protokolliert. |
| 429 | Gültiges `Retry-After` als Mindestwartezeit, unterstützt Sekunden und HTTP-Datum; ohne gültigen Header normaler Backoff. |
| 401 | Uploads stoppen, Reauthentifizierungsdialog in HA. |
| 403, 400/413/415/422, unerwarteter Status, ungültige Bestätigung | Uploads stoppen, Hinweis unter **Reparaturen**. Ursache korrigieren und neu laden bzw. neu konfigurieren. |
| Zertifikatsfehler oder HTTP-Redirect | Uploads stoppen. Zertifikat, Kette, Hostname und Ingress korrigieren. Keine Zertifikatsumgehung und keine Weitergabe von Zugangsdaten an Redirect-Ziele. |

Pairing wird wegen des einmal verwendbaren Codes niemals automatisch wiederholt. Geht eine Pairing-Antwort verloren, einen neuen Code erstellen. Bei `Retry-After` über 24 Stunden stoppt der Sender mit einem Reparaturhinweis, statt früher als angefordert zu senden.

Der Arbeitsspeicherpuffer enthält höchstens 100 Beobachtungen; bei Überlauf wird die älteste verworfen. Beobachtungen über 23 Stunden sowie nach einer rückwärts korrigierten Uhr in der Zukunft liegende Beobachtungen werden verworfen. Event-IDs und Beobachtungsdaten bleiben bei Wiederholungen stabil; nur `sent_at` wird aktualisiert. **Jeder Ingest-Request enthält genau einen Heartbeat im `events`-Array**, auch wenn beim Pairing `max_batch_events: 100` zurückkommt. Dieser Wert ist eine Obergrenze: `PROTOCOL.md` erlaubt 1–100 Events; der Sender bleibt zusätzlich mit dem ursprünglichen Empfänger für genau einen Heartbeat kompatibel. Es werden keine weiteren Eventtypen oder Payload-Felder ergänzt. Die tatsächliche unkomprimierte Bytezahl berücksichtigt die beim Pairing gemeldete Grenze; ist schon ein einzelner Heartbeat zu groß, stoppt der Sender.

Nach Verbindungsausfällen werden wartende Heartbeats einzeln in Beobachtungsreihenfolge bestätigt und aktuelle Beobachtungen ergänzt. Der Rückstau wird mit höchstens 20 Requests pro Minute abgebaut. Laravel kann veraltet anzeigen, bis ein aktueller Heartbeat bestätigt wurde; alte Beobachtungen frischen den Status nicht auf. Bestätigt wird nur eine vollständige, plausibel quittierte Batch. Die Laravel-Deduplizierung schützt bei verlorenen Antworten vor erneutem Speichern. **Kein Exactly-once-Versprechen und kein persistenter Wiederholungspuffer:** Neustart, Neuladen und Optionsänderung verwerfen unbestätigte Daten; danach entsteht ein frischer Heartbeat. Es gibt keinen Export von Entitäten, Zuständen, Attributen, CPU-/RAM-Daten oder Snapshots.

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

Die Tests verwenden echte Home-Assistant-Config-Entries, Flows und Sensorplattformen sowie einen simulierten Laravel-Empfänger über HTTPS mit eigener, ausschließlich in Tests vertrauter CA. Sie benötigen keinen echten Laravel-Server. Die offizielle Testbibliothek `pytest-homeassistant` ist auf HA 2026.11.0 festgelegt. CI führt Tests, Ruff, Paketprüfungen sowie die offiziellen hassfest- und HACS-Actions aus.

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
5. Integration deaktivieren/entfernen: keine weiteren Uploads. Serverseitigen Widerruf bei Bedarf separat ausführen.

## Architektur

`client.py` kapselt HTTPS, Pairing und Antwortvalidierung ohne HA-Abhängigkeit. `sender.py` verwaltet Beobachtungen, Batchbildung, Bestätigungen und Retry-Zustand mit genau einem Worker pro Config Entry. Das Runtime-Objekt liegt in `entry.runtime_data`; Plattformen abonnieren Änderungen, ohne selbst zu pollen oder Netzwerkaufrufe auszuführen. Entry-gebundene Hintergrundaufgaben werden beim Unload und HA-Shutdown abgebrochen.

Neue Datentypen erfordern zuerst eine explizite Erweiterung des Laravel-Protokolls. Danach können Beobachtungsgewinnung und Batchbildung ergänzt werden, während Transport, Credential-Verwaltung, Lifecycle und Fehlerbehandlung bestehen bleiben.

Grundlagen: [HA Config Entries](https://contributors.home-assistant.io/core/integrations/config-entries/), [Reauthentifizierung](https://contributors.home-assistant.io/core/integrations/reauth-reconfigure/), [Marketplace-Integration](https://contributors.home-assistant.io/marketplace/guides/integrations/), [offizielle Validierung](https://contributors.home-assistant.io/marketplace/reference/validation/).
