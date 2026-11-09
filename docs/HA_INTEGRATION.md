# Home Assistant Custom Integration – Snapshot v2 architecture

Snapshot v2 implemented in **0.3.2**, independent Health v1 added in **0.4.0**. [PROTOCOL.md](PROTOCOL.md) is the authoritative Laravel contract; [README](../README.md) covers installation and operation. Work is limited to this repository.

## Local collection and export scope

`collectors.py` reads OS-visible CPU/RAM, the HA data filesystem, and registry/state inventory totals. All OS/filesystem work uses HA's executor. CPU is a normalized delta over two aggregate readings; memory is `(total − available) / total`; storage is `used / total`. No process/container denominator is substituted. Missing or failed values have explicit availability and nulls. Inventory follows the contract's registry/state definitions and stays unknown during startup.

`updates.py` reads public HA state, entity registry and device registry APIs without polling or enabling sources. Native `on` means offered independently of INSTALL. A current skipped version is offered separately; a stale skip on an up-to-date source is metadata only. Supervisor product identities use stable registry unique IDs. The seven categories survive v2 serialization. `update_percentage` supplies numeric install progress when HA reports an installation; legacy numeric `in_progress` is also supported.

Both local discovery and export-all default to true. Export-all forces discovery even if the local discovery preference is false. Existing explicit false values, Core/OS preferences and selected entity IDs remain respected. Disabling export-all yields `scope: selected` with requested Core/OS plus additional selected sources. Every selected record is required. All-source discovery includes hidden active and disabled registry entries; auto-discovered disabled entries can be non-required. Native HA reasons `hass`, `device` and `config_entry` have no v2 reason enum and are exported as `disabled_by: null`, preserving disabled availability and required-source semantics. `user` and `integration` survive unchanged. Sources are never enabled automatically, and a disabled entry's stale state is discarded.

Each source is isolated. An exception produces an unavailable source record, retaining valid peers. Discovery failures preserve already discovered/explicit sources. A 4,096-source cap bounds observations and marks retained records partial when discovery exceeds it. Counts describe represented records only. Unknown/unavailable or required disabled sources prevent complete coverage. Empty complete discovery is a measured zero; unknown/unsupported inventories are not. Complete/partial/unknown/unsupported and all/selected are separate v2 concepts.

Original text is preserved without stripping, normalization, SemVer parsing or truncation. Only contract-safe bounded Unicode text is accepted; an invalid optional value becomes null with a fixed local issue code. Null available version still represents an offered update. Source/device data is allowlisted, including only the opaque device registry ID, name, manufacturer and model. Release notes/URLs, hardware identifiers, arbitrary attributes and config entries are excluded. Diagnostics and entities contain counts/statuses only.

## Coordinator and transport

`MonitoringCoordinator` uses `DataUpdateCoordinator` as the shared data store with automatic polling disabled. An explicit HA `async_track_time_interval` clock starts one entry-scoped background collection every 60 seconds. HA’s `pref_disable_polling` for entity updates and disabled local sensor entities do not stop the outbound agent. Slow collection skips timer ticks instead of queueing tasks. Unload cancels both the clock and current collection; re-enabling creates a fresh collector with an unknown first CPU sample and valid subsequent samples. Every observation receives a fresh UUID and UTC timestamp. Monitoring upload remains opt-in. Resource export selection is applied to a separate view; JSON serialization runs in HA's executor before the local observation is published.

`HeartbeatSender` remains `entry.runtime_data`, owning a separate coordinator/uploader. Pairing and heartbeat ingestion stay on v1 with unchanged schemas and verified shared HA HTTP session. Monitoring always uses `schema_version: 2` and `/api/v2/agents/monitoring`. The removed `monitoring_schema_version` option is cleaned from existing entries on setup, preserving every other option and pairing credentials. There is no monitoring protocol selector or fallback.

V2 sends exactly one entire snapshot per request: at most 4,096 source records and 2,097,152 uncompressed serialized bytes including its envelope, metrics and inventory. A larger-than-v1 collection is still one atomic replacement. Transport overflow stops uploads with a Repairs issue and preserves the receiver's acknowledged projection; it never uploads truncated or empty replacement fragments.

A lock permits one monitoring request in flight. Two immutable serialized observations are retained: pending and latest. New observations coalesce only the latest. Retry UUID, time and contents remain stable; only envelope `sent_at` changes. Observations older than 23 hours or in the future after clock rollback are dropped. Buffers are memory-only and discarded on reload/restart. Exponential backoff/jitter is capped and Retry-After respected. Minimum 15-second spacing bounds attempts at four/minute, below v2's separate six/minute instance quota. Heartbeats are independent.

Permanent HTTP errors, invalid acknowledgments or TLS failures stop the affected uploader with a Repairs issue. No redirect carries credentials. A credential rejection on either path stops both and starts reauthentication; an older in-flight response cannot undo revocation. Entry-scoped tasks/listeners are cancelled on unload. Snapshot success never changes heartbeat/liveness timestamps. Diagnostics expose last observation time and resource collection/export statuses separately from uploader status, without raw update metadata. Bounded HTTP-422 responses retain at most eight allowlisted schema paths in Repairs and diagnostics; arbitrary keys, error messages and rejected values are never retained.

## Validation

Tests use real HA entries, flows, registries, update entities, coordinators and sensor platforms with a strict simulated Laravel receiver over verified TLS. They cover the v2 monitoring schema, unchanged v1 pairing/heartbeats, periodic resource uploads without manual refresh through disable/re-enable (with and without HA polling disabled), all four reported offered updates, raw Shelly versions, read-only Immich/Proxmox, seven categories, selected/all scopes, partial failures, native skip/progress, large atomic collections, byte/source limits, stable retries, quotas, revocation races and unload. The simulated receiver validator is independent of production serializers. Optional tests invoke the real neighboring Laravel FormRequest through Sail, with no controller or database writes; they reproduce the previously rejected `hass` reason and verify all native reasons plus startup/161-source payloads. Run pytest, Ruff, repository validation and official hassfest as described in README. Real hardware and live Laravel deployment acceptance remain an operator test.

- [HA async APIs](https://contributors.home-assistant.io/core/internals/asyncio/)
- [DataUpdateCoordinator](https://contributors.home-assistant.io/core/integrations/fetching-data/)
- [Update entities and features](https://contributors.home-assistant.io/core/entities/update/)
- [HA 2026.11 update state/progress](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/update/entity.py)

No HA source was copied. No Laravel, infrastructure or remote publication changes are included.

## Sprint 6 – independent Health v1

See [Health collection](HEALTH_COLLECTION.md) for the four collectors, explicit consent, native backup evidence and limitations, deterministic report bounds, separate worker/retry lifecycle and validator tests. `HeartbeatSender.health` owns its independent coordinator/uploader; `sender.monitoring` retains its existing v2 behavior. Health creates no local entities. Heartbeat requests now also use a concurrency lock to prevent manual and scheduled sends from racing over the same buffer.
