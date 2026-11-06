# Home Assistant Custom Integration – Sprint 5 architecture

Status: implemented in version **0.2.1**. [README](../README.md) describes installation, configuration, scope semantics and operational limits. [PROTOCOL.md](PROTOCOL.md) remains the authoritative Laravel contract. This repository contains only the HA integration; no Laravel or infrastructure changes belong here.

## Local collection

`collectors.py` collects OS-visible CPU/RAM and the HA data filesystem with psutil, registry/state inventory totals, and explicitly configured update sources. No System Monitor, HACS or additional sensor integration is required for resources or totals. All filesystem and OS work runs in HA's executor. CPU uses two aggregate OS counter readings, excluding double-counted guest time and treating I/O wait as idle; a first/invalid interval is unknown. Memory uses total minus available; storage uses used divided by total. Host, container consumption and HA-process consumption are never substituted for each other. Current resources use only the protocol scopes `host` (visible OS) and `data` (HA data filesystem); there is no process or cgroup-consumption metric in this version.

Entity/device registries include disabled entries. Automation/scene totals count currently present domain states. Integration totals deduplicate successfully loaded component domains, including YAML setups. Inventory and updates stay unknown until HA has finished startup.

Core/OS update sources use the Supervisor integration's public cached information and stable entity-registry unique IDs. `after_dependencies: [hassio]` arranges ordering when Supervisor is present without requiring it on Container/Core installations. The default automatic discovery includes all enabled/hidden `update.*` states and registry coverage, regardless of INSTALL capability. Explicit selection can additionally require particular sources or replace discovery when it is switched off. Disabled sources are observed as disabled without enabling them; automatically discovered disabled sources are excluded from active coverage, whereas explicitly monitored disabled sources make coverage incomplete. Native update availability, including skipped versions, is used instead of ordering arbitrary version strings. There is no separate external version lookup on Container/Core. A missing or failed monitored source makes coverage incomplete. `updates.py` preserves an immutable local inventory, including device/source/version metadata and install capability, independently of the v1 wire projection. Only offered updates are checked against v1 version syntax; an up-to-date Shelly source with an opaque build ID cannot suppress other offered updates. Offered build IDs outside v1 syntax still require an unavailable wire collection without silently rewriting versions. Unsupported OS sources are never fabricated. Names are fixed product names or selected entity IDs, avoiding private friendly names and arbitrary attributes.

## Shared state and optional entities

`MonitoringCoordinator` in `monitoring.py` uses HA's `DataUpdateCoordinator` and a permanent subscription to collect every 60 seconds even with all monitoring entities disabled. It holds one common observation for local sensors and subsequent export. Each cycle creates a fresh UUID and UTC timestamp; per-collector failures produce explicit statuses/nulls instead of stale values or fabricated zeroes. Optional diagnostic sensors default to disabled and read this common state without I/O.

Monitoring upload is opt-in. Options select exported resources, Supervisor Core/OS sources, automatic update discovery (on by default), and additionally required update entities. Local discovery is capped at 4,096 sources; v1 is limited to 100 offered items. Disabling transmission preserves collection. Disabling/unloading the integration stops its collection as part of the normal HA lifecycle.

## Independent transport

The existing `HeartbeatSender` remains the config entry runtime object; its `monitoring` member owns the coordinator and a separate `MonitoringUploader`. Heartbeats continue through `/ingest` with their frozen event schema. Monitoring uses `/monitoring` with complete Snapshot v1 observations and its own scheduling/backoff. Both reuse the verified HA session and token; redirects with credentials are forbidden. Initial credential failure leaves the entry loaded for local collection and starts reauthentication.

There is at most one monitoring request in flight and two retained serialized observations: an immutable pending snapshot and the latest coalesced snapshot. New observations cannot mutate retries. After recovery, the latest observation follows the pending one. Requests contain one full snapshot, bounded at 65,536 actual bytes including the envelope. Oversized update state becomes explicitly unavailable rather than a silently truncated list. Monitoring uses at most four requests/minute, leaving quota for the existing heartbeat backlog (20/minute). Retries use capped exponential backoff/jitter and respect valid Retry-After values. Observations older than 23 hours or in the future after clock rollback are discarded. There is no persistent retry queue or lossless history guarantee.

Monitoring 404/409 or another permanent rejection stops monitoring with a Repairs issue; local collection and heartbeats continue. A credential rejection stops both outbound paths and starts reauthentication. Snapshot success never changes heartbeat/liveness timestamps. Entry-scoped tasks, subscriptions and timers are cancelled on unload. Diagnostics expose only allowlisted counts/statuses/times, never tokens, pairing codes, server URLs or raw source metadata.

## Validation and references

Tests use real HA entries, registries, coordinators, entity platforms and flows with simulated OS/Supervisor data and a strict TLS backend. Run the commands in README, including official hassfest. Physical HA OS/Container/Core acceptance against real hardware remains an operator test; the automated tests do not claim hardware coverage.

- [Async in HA](https://contributors.home-assistant.io/core/internals/asyncio/)
- [Fetching data / DataUpdateCoordinator](https://contributors.home-assistant.io/core/integrations/fetching-data/)
- [HA entity registry](https://contributors.home-assistant.io/core/integrations/entity-registry/)
- [System Monitor reference implementation](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/systemmonitor/coordinator.py)
- [Supervisor update identities](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/hassio/entity.py)
- [psutil BSD-3-Clause license](https://github.com/giampaolo/psutil/blob/master/LICENSE)

See [update collection analysis and Laravel handoff](UPDATE_COLLECTION.md) for the end-to-end v1 test boundary, exact metadata limitations, and the required future backend/API/UI changes. No live Laravel source or UI was accessed.

No HA source was copied or vendored. HACS/Marketplace publication, remote infrastructure changes and Git commits require a separate user request.
