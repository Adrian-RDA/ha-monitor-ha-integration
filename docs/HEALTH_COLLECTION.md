# System Health Report v1 – Sprint 6

Implemented in 0.4.0. The [Laravel contract](PROTOCOL.md#system-health-report-v1-sprint-6) is authoritative. Health is independent of heartbeat ingestion and Monitoring Snapshot v2. It neither creates local sensors nor calculates a health score.

## Consent and privacy

`health_enabled` defaults to false for new and existing entries. Once enabled, `health_sections` explicitly approves repairs, config entries, unavailable entities and/or native backup observations. Deselected sections are transmitted as `unsupported`, with null metadata and no findings. Disabling Health stops its collection as well as export; the general transmission switch pauses its upload while local Health collection continues.

Repairs and config-entry collection use `scope: all` within the approved section. Entity export defaults to `scope: selected`, with an empty selection. `health_entities` approves individual entity IDs; `health_all_entities` separately approves all enabled entities with an actual unavailable state, including hidden entities and entities without a registry entry. `health_devices` separately approves associations with opaque local device registry IDs. These options never enable an entity or integration.

All `name` fields are null. Source IDs and entity IDs can themselves describe private installation details; this is explained in the configuration choices. Only the contract's allowlisted fields leave HA. Config-entry data/options, repair placeholders/data, entity attributes, friendly names, device hardware identifiers, logs, exceptions and backup contents/paths/keys are never included. Diagnostics expose quality, uploader status and timestamps without records or source IDs. Existing local sensors and pairing storage remain unchanged.

## Collectors and source semantics

`health_collectors.py` contains transport-independent collectors:

- Repairs use `homeassistant.helpers.issue_registry.async_get`. Only active registry issues are included. Deleted or persisted-but-not-reactivated issues are absent. `dismissed_version` identifies ignored issues, matching HA's Repairs API. The original critical/error/warning severity is retained. Hidden issues associated with disabled entries remain repair observations; they do not manufacture config-entry failures.
- Config entries use `hass.config_entries.async_entries`. Loaded and deliberately disabled entries are excluded. Native `setup_error`, `setup_retry`, `migration_error`, `failed_unload`, `not_loaded`, `setup_in_progress` and `unload_in_progress` are preserved. Unknown future states cause partial coverage, never an invented failure. YAML-only failures are outside this contract.
- Unavailable entities use HA's state machine and entity registry. Only actual `unavailable` states on enabled entities qualify. `unknown`, missing states and ordinary `off` are not findings. A disabled registry entry's stale unavailable state is excluded. A missing selected state is not relabeled unavailable. Optional device association uses the device registry's local ID; the associated device may also own working entities. No device-offline assertion or installation-wide affected-device count is produced.
- Backup uses the existing native backup manager's public getter and event subscription. No extra integration, manually configured sensor, Supervisor HTTP request, filesystem scan or network call is required. The agent never loads the backup integration on behalf of the user. The manifest's `after_dependencies: backup` only orders setup when that integration is already configured.

Before `CoreState.running`, the three registry/state collections are `unknown`; startup is not an empty healthy inventory. Each section and individual source is isolated. An unreadable record yields partial coverage and an unknown total while retaining known peers. A failed entire section yields `unavailable`, null total and no stale findings. Unselected or unsupported sections are explicit neutral observations.

Source keys preserve contract-safe local identities, normally `domain:issue_id`, config-entry ID and device registry ID. Nonconforming keys use a deterministic SHA-256 digest salted by the paired instance UUID, without reading credentials.

## Backup evidence and limitations

The native API was inspected against HA **2026.10.4 and 2026.11.0**. Tests exercise the real installed 2026.11.0 `BackupManager`, `async_get_manager`, `async_subscribe_events`, `async_on_backup_event` and native event classes. A test also invokes the public `async_create_backup` failure path with a simulated failing backend. The 2026.10.4 source has the same relevant event/getter/subscription API; a full 2026.10 runtime test is not claimed.

`CreateBackupEvent` has `state`, `stage` and bounded native `reason`, but **no event timestamp**. The synchronous callback records UTC time when the native manager emits the initial `in_progress` event with `stage=None`. Intermediate stages do not advance attempt start. A terminal `completed` event supplies the observed completion time and status `ok`; a terminal `failed` event supplies `failed`. Both manual and automatic creations through that manager use this lifecycle. Restore, receive/upload and idle events do not imply successful creation. A running backup remains running without an arbitrary timeout policy.

Only complete lifecycles observed after subscription are classified. Initial load, reload, manager replacement or attachment during an existing run yields `unknown`, with null metadata. A successful completion observed before a later attempt is retained in memory as `last_success_at`, including across a subsequent failure. It means the last **known observed** successful completion; unobserved earlier successes cannot be reconstructed. Clock rollback that would invert timestamps or put them in the future clears the observation to unknown. Unload removes subscriptions and discards all in-memory backup evidence.

HA's `last_attempted_automatic_backup` and `last_completed_automatic_backup` cover automatic backups only. They do not establish the latest manual attempt/outcome. The manager's `last_action_event` has no start/completion timestamps. Backup inventory dates are creation metadata, not proof that the latest attempt completed successfully. These sources are deliberately not combined into a purported complete history. Empty history does not prove `never`. External backups bypassing the native manager are outside the approved source.

Absent backup integration/API support yields `unsupported`; an expected manager that is temporarily unavailable yields `unavailable`. A loaded manager without sufficient evidence yields `unknown`. Exact native `backup_agent_unreachable` maps to `connection`; generic manager, reader/writer, upload and other failures map to `unknown`, because their cause cannot be classified more precisely from that code. Free-form text is never inspected, stored or exported. Other contract error categories remain available for future sources with reliable typed evidence. No overdue-backup or inferred-success rule is added.

References:

- [Native manager, HA 2026.11.0](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/backup/manager.py)
- [Native manager, HA 2026.10.4](https://github.com/home-assistant/core/blob/2026.10.4/homeassistant/components/backup/manager.py)
- [Native backup getter](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/backup/__init__.py)
- [Repairs API](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/repairs/websocket_api.py)
- [HA async APIs](https://contributors.home-assistant.io/core/internals/asyncio/)

## Report bounds and transport

`HealthCoordinator` schedules an immediate observation followed by a full observation every **300 seconds**, independently of HA entity polling, local sensors, heartbeat and monitoring workers. At most one collection task runs; slow ticks are skipped. Registry/state APIs execute on HA's event loop; JSON projection/encoding uses its executor. Each observation gets a new UUID and original UTC observation time.

Collectors keep a deterministic sorted subset using a bounded heap: **100 repairs, 256 config entries, 1,000 unavailable entities**. Collection overflow yields `partial` with the known full matching total, or null if that total is unreliable/outside the contract bound. Totals describe findings, not the entire HA inventory. Complete empty scope means a real zero within that scope.

`fit_report` measures the actual encoded envelope and trims the tail of unavailable entities, then config entries, then repairs until it fits **262,144 uncompressed bytes**. Each affected section becomes `partial` while preserving its known total. The local observation remains intact. There is one full replacement report per request, with every section and required null field present; no chunks or fragments.

`HealthUploader` reuses the existing verified client, acknowledgment validation and bounded worker mechanics. It has its own pending/latest frozen bodies, lock, timer, failure counter and backoff. Health uses `/api/v1/agents/health`, `schema_version: 1`; Snapshot v2 continues on its v2 endpoint. Two immutable reports maximum are retained; new unsent observations coalesce. Only `sent_at` advances on retry. Retry IDs, timestamps and content stay unchanged. Observations older than 23 hours or future-dated after a clock rollback are discarded.

Minimum request spacing is 15 seconds (at most four attempts/minute). Backoff uses bounded exponential jitter with a 900-second cap; longer valid `Retry-After` values are respected up to the shared 24-hour bound. Permanent errors, including 409, stop only Health and create a translated Repairs issue. Credential rejection starts shared reauthentication and prevents all further authenticated uploads; older in-flight acknowledgments cannot undo it. Verified shared HA HTTPS, bounded responses, no redirects with credentials and no payload logging remain in force.

## Verification

Run the full commands in [README](../README.md#entwicklung-und-tests), including official hassfest. Health tests cover native registry semantics, all entry states, disabled/stale entities, privacy choices, source isolation, startup, deterministic caps, actual byte overflow, backup events/errors/reload/clock changes, periodic disable/re-enable, separate upload failures, immutable duplicate retries, Retry-After, expiry, revocation races and cancellation. The TLS backend independently validates the Health wire shape and byte bounds.

Optional `HA_MONITOR_LARAVEL_PATH=/home/adrian/ha-monitor .venv/bin/pytest tests/test_laravel_contract.py` invokes the installed Laravel `HealthReportRequest` and existing `MonitoringSnapshotV2Request` through already running Sail. It only validates generated payloads; it calls no controller and writes no application data. Health cases include full/selected/startup collections, overflow and running/ok/failed backups.
