# HA Monitor - Agent / Server Protocol v1

Status: **Heartbeat receiver implemented in Sprint 2; Monitoring Snapshot v1 in Sprint 5; independent Monitoring Snapshot v2 implemented on 2026-11-07.** This is the contract for the separate Python integration repository. No Python sender is included here. Arbitrary `entity_state` exports and automatic token rotation are not implemented. The pairing response and heartbeat contract below remain unchanged.

## Transport and authentication

All agent requests require HTTPS, JSON objects, `Content-Type: application/json`, and uncompressed bodies. Plain HTTP returns 403, including on localhost; tests simulate HTTPS through Laravel's request boundary. Configure a valid TLS ingress and explicit trusted proxy addresses before a real installation. Never disable certificate validation or trust arbitrary forwarded headers.

These routes are stateless and independent of browser login, CSRF, Fortify and Spatie. Browser login never substitutes for an agent token. Credentials identify the instance; the request body cannot select another instance. No HA administrative token or inbound HA connection is used.

| Method | Endpoint                    | Authentication                                                            |
| ------ | --------------------------- | ------------------------------------------------------------------------- |
| POST   | `/api/v1/agents/pair`       | One-time code in JSON                                                     |
| POST   | `/api/v1/agents/ingest`     | `Authorization: Bearer <agent_token>`                                     |
| POST   | `/api/v1/agents/monitoring` | `Authorization: Bearer <agent_token>`                                     |
| POST   | `/api/v2/agents/monitoring` | `Authorization: Bearer <agent_token>`; independent snapshot schema/limits |

### Pairing

An authorized web user with `instances.pair`, instance scope, confirmed administrative TOTP and authentication confirmation within 15 minutes creates a code in the instance detail screen. The code is 32 lowercase hexadecimal characters (128 random bits), valid for at most ten minutes. Creating it explicitly invalidates the previous code **and immediately revokes the previous agent token**. Disabled instances cannot issue or redeem codes. Revocation cancels both credentials and pending codes. Deactivation does the same; reactivation requires explicit new pairing.

The browser receives the code once through a private/no-store JSON response, using Inertia `useHttp`; it is not placed in page props, page history or a plaintext session flash. Losing it requires issuing a new code. Only a SHA-256 hash and expiry are stored on the instance.

Pair request:

```json
{ "code": "<32-character one-time code>" }
```

Successful pairing returns HTTP 200:

```json
{
    "schema_version": 1,
    "instance_id": "7e9dcd76-5fb2-4ee1-8ee8-316f6d498fc1",
    "agent_token": "<64 lowercase hexadecimal characters>",
    "heartbeat_interval_seconds": 60,
    "max_batch_events": 100,
    "max_body_bytes": 65536
}
```

The token contains 256 random bits. Store it in the HA config entry, never diagnostics or logs. Laravel keeps only its SHA-256 hash; there is one current credential per instance. The token has no automatic expiry; revocation, deactivation and a new pairing-code issuance invalidate it. The pairing row is locked through consumption and issuance, preventing parallel reuse. Wrong, expired, consumed or disabled codes return the same 401. Malformed codes return 422. Pairing is limited to five requests per minute per source IP, including unsuccessful attempts.

### Heartbeat ingestion

```json
{
    "schema_version": 1,
    "instance_id": "7e9dcd76-5fb2-4ee1-8ee8-316f6d498fc1",
    "integration_version": "0.1.0",
    "sent_at": "2026-11-04T18:00:00Z",
    "events": [
        {
            "event_id": "5e65e0bd-a149-4484-98f5-551f05df8464",
            "type": "heartbeat",
            "occurred_at": "2026-11-04T17:59:58Z",
            "data": { "ha_version": "2026.11.0" }
        }
    ]
}
```

- `schema_version` is the JSON integer `1`. `instance_id` and `event_id` are UUID strings. The instance UUID must match the credential (403 otherwise).
- Only the envelope fields shown above, the four event keys and `data.ha_version` are accepted. Additional data and event types return 422. This deliberately conservative contract requires explicit versioning/compatibility review before extension.
- Versions are required ASCII strings of 1–32 characters, beginning with a digit; subsequent characters may be letters, digits, `.`, `+`, `_` or `-`. No arbitrary state, attributes, entity data, credentials or diagnostics are accepted.
- Timestamps require RFC3339 form with explicit `Z` or numeric offset. Up to six fractional second digits are accepted and preserved for event ordering. Sent/event times must be within the last 24 hours and at most five minutes ahead of server time. Event time must not exceed `sent_at`.
- A batch contains 1–100 events with distinct IDs. The complete batch is validated before writes. The actual uncompressed body is limited to 65,536 bytes; HTTP `Content-Length` is not trusted as the only check. Excess bytes return 413, invalid JSON/depth over 16/non-object roots return 400, and unsupported content type/encoding returns 415.
- Ingestion is limited to 120 requests/minute/source IP before authentication and 30 requests/minute/authenticated instance. Limits are shared through the application's cache store; grouping by IP can affect installations sharing NAT.

HTTP 200 means the complete validated batch has committed to PostgreSQL, including current-instance metadata. No volatile queue handoff is acknowledged. Example response:

```json
{ "accepted": 1, "duplicates": 0, "received_at": "2026-11-04T18:00:00+00:00" }
```

`accepted` counts newly stored events. A repeated `(instance, event_id)` is acknowledged as a duplicate and never overwrites the original observation, even if its otherwise valid payload differs. Whole-request retries are safe. Row locking and the database unique constraint serialize concurrent ingestion for one instance. Revocation and active status are checked again while holding that same lock.

`last_contact_at` records server receipt of every successful valid batch, including duplicates. `last_heartbeat_at` and current HA/integration versions advance only for a strictly newer, newly accepted event. Out-of-order events remain in history without replacing current state. Replayed old/duplicate data cannot freshen the heartbeat status. A re-pair resets current monitoring fields; history and deduplication keys survive.

## Status, retention and retry behavior

- Recommended sender heartbeat: 60 seconds, as returned at pairing. No requirement for Laravel to poll HA.
- Status priority: disabled; unpaired; paired/waiting for first heartbeat; stale if the latest event time is more than 180 seconds old; otherwise online. Server-clock tolerance allows up to five minutes of positive sender skew, which can correspondingly extend perceived freshness.
- Cockpit and detail status derive from timestamps at read time, avoiding stored status/scheduler races. Browser monitoring data refreshes every ten seconds. The scheduler runs `monitoring:prune` hourly to remove expired code hashes and up to 10,000 events received more than 30 days ago per run. At high sustained volume cleanup can have a backlog; this is a bounded MVP housekeeping policy, not a hard deletion deadline. Current instance metadata remains available.
- Deduplication lasts for the retained event history (normally 30 days), longer than the 24-hour accepted replay window. Reusing an old event ID with a new timestamp after retention is outside the sender contract: generate a fresh UUID for every new observation.
- Configuration defaults live in `config/monitoring.php`. Changing transport bounds is a protocol decision; coordinate with deployed senders.

| Response              | Sender behavior                                                                   |
| --------------------- | --------------------------------------------------------------------------------- |
| 200                   | Batch durably stored; remove acknowledged items from buffer                       |
| 400 / 413 / 415 / 422 | Correct or split the payload; do not retry unchanged                              |
| 401                   | Invalid/revoked credential or pairing code; stop uploads and reauthenticate       |
| 403                   | HTTPS missing or instance mismatch; correct setup, never disable TLS verification |
| 429                   | Respect `Retry-After` and retry with bounded delay                                |
| 5xx / timeout         | Exponential backoff with jitter and cap; retry stable event IDs                   |

Sender buffers/retries must remain bounded and must not block HA. Durable replay across HA restarts, entity exports, supported HA versions and sender-side reconciliation remain separate integration work. Do not claim exactly-once delivery.

## Monitoring Snapshot v1 (Sprint 5)

`POST /api/v1/agents/monitoring` is an additional, independently versioned contract. Its `schema_version: 1` identifies **Monitoring Snapshot v1**, not an extension of the heartbeat event schema. Do not send monitoring fields or snapshot events to `/ingest`; that endpoint continues to reject them. Pairing, credentials, heartbeat timing, liveness classification and heartbeat responses are unchanged. Existing installations can keep sending only heartbeats indefinitely; their monitoring values display as unknown.

The same current per-instance bearer token authenticates both endpoints. Monitoring uses the same HTTPS/uncompressed JSON middleware, 65,536-byte actual body limit and depth limit of 16. The existing 120 requests/minute/IP ingress quota and 30 requests/minute/instance authenticated quota are **shared with heartbeat ingestion**, not additional allowances. A monitoring request never changes `last_contact_at`, `last_heartbeat_at` or HA/integration versions; a fresh monitoring snapshot cannot hide an overdue heartbeat.

### Complete request example

```json
{
    "schema_version": 1,
    "instance_id": "7e9dcd76-5fb2-4ee1-8ee8-316f6d498fc1",
    "sent_at": "2026-11-06T19:00:01Z",
    "snapshots": [
        {
            "snapshot_id": "10e1443d-573e-4c53-9393-51136268e410",
            "observed_at": "2026-11-06T19:00:00.123456Z",
            "metrics": {
                "cpu_percent": {
                    "status": "ok",
                    "value": 12.5,
                    "scope": "host"
                },
                "memory_percent": {
                    "status": "ok",
                    "value": 48,
                    "scope": "host"
                },
                "storage_percent": {
                    "status": "unsupported",
                    "value": null,
                    "scope": null
                }
            },
            "inventory": {
                "entities": { "status": "ok", "value": 1250 },
                "devices": { "status": "ok", "value": 80 },
                "automations": { "status": "ok", "value": 0 },
                "scenes": { "status": "unknown", "value": null },
                "integrations": { "status": "ok", "value": 35 }
            },
            "updates": {
                "status": "ok",
                "items": [
                    {
                        "key": "update.home_assistant_core",
                        "kind": "core",
                        "name": "Home Assistant Core",
                        "installed_version": "2026.11.0",
                        "available_version": "2026.11.1"
                    },
                    {
                        "key": "update.home_assistant_os",
                        "kind": "os",
                        "name": "Home Assistant OS",
                        "installed_version": null,
                        "available_version": "17.0"
                    },
                    {
                        "key": "update.zigbee",
                        "kind": "other",
                        "name": "Zigbee",
                        "installed_version": "1.0",
                        "available_version": "1.1"
                    }
                ]
            }
        }
    ]
}
```

All four envelope fields and all five snapshot fields shown are required. Only these fields are accepted. `snapshots` is a JSON list of **1–20** complete snapshots with distinct UUIDs, case-insensitively. Unknown envelope, snapshot, metric, inventory or update keys return 422. This is a full replacement protocol, not a patch: every snapshot must contain all three metrics, all five inventory counters and the update collection. Missing fields never silently mean zero, unchanged or an empty update list. New domains or incompatible semantics require a separate compatibility/version decision.

`sent_at` and each `observed_at` have the same RFC3339, explicit timezone, six-digit maximum fractional precision, 24-hour replay and five-minute future-skew bounds as heartbeat timestamps. Each `observed_at` must be at or before `sent_at`. UUIDs are normalized to lowercase; timestamps normalize to UTC and preserve microseconds. `schema_version` must be the JSON integer `1`.

### Availability and numeric observations

Each metric/counter is an object with required `status` and required `value` (including explicit JSON null). Metrics additionally require `scope` (including explicit null).

| Status        | Meaning                                                                            | Value                            |
| ------------- | ---------------------------------------------------------------------------------- | -------------------------------- |
| `ok`          | Successfully collected for this snapshot                                           | Required JSON number; never null |
| `unknown`     | No reliable observation yet, or collection completeness is unknown                 | Must be null                     |
| `unavailable` | Normally supported, but collection failed or the source is temporarily unavailable | Must be null                     |
| `unsupported` | The installation/source cannot provide this observation                            | Must be null                     |

`ok` with zero means a real measured zero. Unknown/unavailable/unsupported must never be converted to zero. Null values replace any previously known value in the current projection. A failed collection must not reissue an old value with a fresh observation timestamp. No arbitrary error messages or diagnostics are accepted.

Resource values are finite JSON numbers from **0 through 100 inclusive**, with decimal fractions allowed; numeric strings and booleans are rejected. No percentages are inferred from counts or heartbeat metadata. Raw bytes, load averages, temperatures, mount paths and arbitrary sensors are outside v1.

| Metric            | Unit and meaning                                                                                                                    | Allowed scope              |
| ----------------- | ----------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| `cpu_percent`     | Used CPU capacity / total effective CPU capacity × 100 over the sampling interval, normalized across logical CPUs; not load average | `host` or `home_assistant` |
| `memory_percent`  | Host: (total − available) / total memory × 100. HA process: resident memory / effective environment memory limit × 100              | `host` or `home_assistant` |
| `storage_percent` | Used bytes / total bytes × 100 of the selected filesystem                                                                           | `host` or `data`           |

`host` means the measured operating environment (including a container when that is the only visible environment), never an inferred physical host. `home_assistant` means only the HA process, shown as **HA-Prozess**; CPU uses the environment's effective logical CPU capacity as denominator. `data` means the filesystem containing HA's data directory, shown as **HA-Datenpartition**. For storage `host` means the main filesystem of the measured environment. Do not combine volumes or sum percentages. The sender must know the numerator, denominator and measurement scope; otherwise report a non-ok status. A known scope may accompany a non-ok metric; null scope means unknown. An `ok` metric requires a non-null allowed scope. The UI keeps scopes visible and does not average unlike scopes across installations.

Inventory values are JSON integers from **0 through 1,000,000 inclusive**, without numeric-string, boolean or fractional coercion:

| Counter        | Definition for this contract                                                                                                                             |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `entities`     | Number of entity registry entries, including disabled entries; not the size of the state machine                                                         |
| `devices`      | Number of device registry entries                                                                                                                        |
| `automations`  | Number of currently present `automation` domain states, including off/disabled automations; do not count executions                                      |
| `scenes`       | Number of currently present `scene` domain states                                                                                                        |
| `integrations` | Distinct integration domains successfully set up, including YAML-based integrations where discoverable; multiple config entries of one domain count once |

A sender unable to implement a definition reliably reports unknown/unsupported instead of substituting a different count. Only totals leave HA; do not export registry records, entity/device names, config entry data, entity attributes or integration configuration.

### Available updates and priority

`updates` always contains `status` (the same four statuses) and `items` (a JSON list of **0–100** records). `ok` plus `[]` means collection succeeded and **no available updates** were found. Non-ok status requires `[]` and means the update situation is unknown/unavailable/unsupported, never “up to date”. `ok` requires a complete inventory of the explicitly configured update sources. If a source fails, the known subset must not be represented as complete; send `unavailable` and no items. If more than 100 updates are available, send `unavailable`/`[]` until the contract is expanded; do not silently truncate or split a full collection across snapshots.

Each item must contain exactly these five keys:

| Field               | Validation and meaning                                                                                                                                                           |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `key`               | Stable, unique within this snapshot; 1–128 lowercase ASCII characters matching `[a-z0-9][a-z0-9_.-]*`; an explicitly selected update entity ID or a stable synthetic product key |
| `kind`              | `core`, `os` or `other`; at most one Core and one OS item per snapshot                                                                                                           |
| `name`              | Display name, 1–120 Unicode characters, no Unicode control/format characters; must not contain secrets                                                                           |
| `installed_version` | Required key, null if unknown, otherwise 1–64 ASCII characters matching `[0-9A-Za-z][0-9A-Za-z.+_-]*`                                                                            |
| `available_version` | Required nonempty version using the same 1–64-character syntax                                                                                                                   |

Only actually available updates are sent. Core means Home Assistant Core, OS means Home Assistant OS; Supervisor, apps/add-ons, HACS/custom integrations, firmware and all remaining selected update sources use `other`. An unsupported OS is not a missing update; never create a fake OS item. The sender classifies products; Laravel does not guess priority from names/keys or compare arbitrary version strings. UI and stored states order Core first, OS second, then other records by stable key. Core and OS are both visually prioritized, without asserting a security vulnerability. No release URLs, release notes, config attributes, device inventory or raw update entity state are accepted.

### Acknowledgment, idempotency and ordering

Success is HTTP 200 with exactly the same three response field names as heartbeat acknowledgment:

```json
{ "accepted": 1, "duplicates": 0, "received_at": "2026-11-06T19:00:01+00:00" }
```

The receiver validates the **entire** request before writing, locks the instance row, rechecks active status/current credential, then commits all receipts, metric samples and applicable state changes synchronously to PostgreSQL. It does not acknowledge a Redis-only handoff. Any validation, authentication, identity or conflict failure leaves the whole batch uncommitted. `accepted` counts new snapshot IDs, including accepted late observations; `duplicates` counts retained IDs with identical normalized snapshot content.

- Generate one new UUID per new observation. Retrying uses the **same ID, observation time and content**. Envelope `sent_at` may change on retry within timestamp bounds.
- The unique deduplication key is `(instance, snapshot_id)`, independent of heartbeat event IDs. It survives token rotation/re-pairing. Monitoring receipts store a SHA-256 hash of normalized snapshot content rather than a second copy of every snapshot.
- Object key order, update item order, UUID letter case, equivalent timezone representation and trailing timestamp fractional zeros do not change identity. JSON integer/decimal representations that encode to the same normalized number are equivalent. Array order is otherwise retained; item keys are case-sensitive and must be lowercase.
- **409** means a retained snapshot ID was reused with different content. The complete batch rolls back. Stop retrying that conflict unchanged; fix the sender's ID/content handling. Never mutate acknowledged observations. This stricter conflict behavior applies only to the new endpoint; heartbeat duplicate semantics remain unchanged.
- Snapshots within a batch are processed by UTC observation time, then lowercase UUID. Across requests, only a **strictly newer** observation updates current metrics/state. Equal timestamps keep the already projected state; late/equal snapshots still store their metric sample and receipt, but do not insert historical inventory/update states. This is a history of accepted forward state transitions, not a reconstruction of every late-arriving snapshot.
- A new forward snapshot with unchanged inventory/update state advances current observation/receipt times and metrics without adding a state-history row. Update reordering and object key reordering are not changes. A later return to an earlier state is a real transition and creates an entry. Identical metric readings at different times remain useful samples, while retries create no extra samples.
- A duplicate never refreshes current monitoring observation/receipt timestamps. Snapshot freshness is read-time sender-time age: more than **600 seconds** means stale; exact boundary is still fresh. Retained data stay visible as the last known snapshot. Future sender clock skew can extend freshness by at most the allowed five minutes. This status is independent of the heartbeat's 180-second contact status and is not an overall health verdict.
- Re-pairing clears the current monitoring projection as well as the established heartbeat metadata, retaining history and receipts. A replayed old ID cannot recreate current state; send a fresh snapshot with a new ID. Revocation/deactivation retain last known monitoring state for authorized viewing, visibly alongside instance operation/contact status.

The response/error table above applies, with the added non-retryable 409 conflict case. 422 is also returned for incomplete snapshots, invalid availability/value combinations, duplicate update keys, repeated Core/OS entries, timestamps after `sent_at` or duplicate snapshot IDs in one batch. Unknown schema versions are rejected rather than guessed. Invalid JSON/root/depth returns 400. Credential failure returns 401; wrong instance or missing HTTPS returns 403; oversize returns 413; encoding/content type returns 415; shared quota exhaustion returns 429 with `Retry-After`.

### Storage, retention and sender implementation checklist

| Storage                    | Purpose                                                                                                           | Default retention                                |
| -------------------------- | ----------------------------------------------------------------------------------------------------------------- | ------------------------------------------------ |
| `monitoring_currents`      | One current projection per instance: metrics, inventory/update state and latest applied observation/receipt times | Until new snapshot, re-pair or instance deletion |
| `monitoring_samples`       | One three-metric sample per newly accepted snapshot, including explicit missing-value statuses and scopes         | 30 days by server receipt time                   |
| `monitoring_state_changes` | Inventory/update state only when a forward observation changes its normalized content                             | 90 days by server receipt time                   |
| `monitoring_receipts`      | UUID/content-hash idempotency ledger; no raw full-payload archive                                                 | 30 days by first server receipt time             |

All four tables cascade on instance deletion. Hourly `monitoring:prune` deletes at most **10,000 rows per history/receipt table per run**, in addition to the existing heartbeat cleanup. This is a bounded operational policy, not a guaranteed deletion deadline under sustained load; monitor backlog and schedule additional bounded runs if necessary. Current projections survive history pruning. History screens enforce their retention windows even if cleanup backlogs. Receipt retention exceeds the accepted replay window, so valid old retries remain deduplicated. Reusing expired IDs with new observation times is outside the contract. Keep retention changes coordinated with the 24-hour replay limit. Defaults are in `config/monitoring.php`.

Recommended sender behavior:

1. Keep the existing 60-second heartbeat sender unchanged. Add a separate optional monitoring collector/sender, normally one complete snapshot every 60 seconds; a slower cadence up to five minutes is acceptable. No new monitoring capability appears in the frozen pairing response.
2. Explicitly configure exported metrics and update sources. Report unsupported/unknown for unselected or inaccessible data; do not export all HA states or attributes. Use async, non-blocking collection/HTTP, bounded buffers, bounded retries and clean unload in the separate integration repository.
3. Collect counts/updates locally, coalesce observations, and keep metric scope honest for the installation type. Detect pending startup/failed collection with status rather than fabricated zeroes. Freeze a snapshot once assigned its UUID.
4. Measure the serialized UTF-8 body; use at most 20 snapshots and 65,536 bytes including the envelope. Split only between complete snapshots. If one complete snapshot cannot fit, bound selected source names/keys and report unavailable updates rather than sending partial state. Never drop required fields.
5. Respect the combined heartbeat/monitoring quotas. Retry temporary failures with exponential backoff/jitter, cap delays/buffer size, discard observations outside the replay window, and send a new full snapshot on recovery. Redis/receiver unavailability must not block HA core.
6. Deploy against this explicit endpoint contract; 404 means receiver capability is absent and monitoring should pause until compatible receiver configuration/update, while heartbeats continue. A 422 version rejection requires compatibility correction, not a TLS/auth bypass. Verify the separate sender against these fixtures/negative cases before real-device release.

Cockpit loads at most five latest ACL-visible instance snapshots; it does not present partial coverage as fleet-wide averages. Instance details show current data, the latest 60 retained metric samples and the latest ten state changes. The existing ten-second browser partial reload includes these props, rechecking instance authorization each time. No extra browser API, HA polling, health-score inference or new package/service is introduced.

## Local verification

```bash
vendor/bin/sail artisan test --compact tests/Feature/AgentProtocolTest.php tests/Feature/AgentMonitoringTest.php tests/Feature/InstanceAccessTest.php tests/Feature/InstanceManagementTest.php
```

These tests use the separate PostgreSQL `testing` database and simulated HTTPS; no live HA integration or exposed receiver is required. Real HA-to-Laravel testing needs a separately configured valid HTTPS endpoint. Tests do not configure ingress; consult actual Compose bindings and STATUS.md for the operator's private-LAN setup. Sprint 5 adds no TLS bypass, host-port binding or infrastructure service.

## Monitoring Snapshot v2 (2026-11-07)

**Implemented receiver:** `POST /api/v2/agents/monitoring`, `schema_version: 2`. This is independent of Monitoring Snapshot v1. Pairing and heartbeat endpoints/responses and v1 snapshot validation, limits, replacement and retry semantics remain unchanged. The HA agent 0.2.3 described in the integration repository's `docs/UPDATE_COLLECTION.md` still sends v1; sender implementation/rollout follows separately. There is no automatic fallback or negotiated capability in the frozen pairing response.

### Transport and larger collections

Use the same per-instance bearer token and verified HTTPS, uncompressed JSON object and depth limit 16. V2 permits **2,097,152 actual uncompressed bytes**, **one** snapshot per request and **0–4,096 update source records**. Count every serialized UTF-8 byte, including the envelope, metrics, inventory and metadata. The byte and record limits both apply; reaching one does not waive the other. Oversized requests fail with 413, excessive records/multiple snapshots with 422, before any replacement. Reverse proxies must permit this bounded body on the v2 route; no proxy/infrastructure configuration is changed by this implementation.

This contract deliberately uses a **single atomic collection**, rather than the optional chunk staging approach discussed in UPDATE_COLLECTION.md: it supports more than 100 records/64 KiB without a staging store, chunk expiry, partial assembly or another deployable component. The snapshot UUID identifies the entire collection. Never split a larger collection into independently replacing snapshots, silently truncate it or mark a transport-truncated list complete. If it cannot fit these declared bounds, retain the previous acknowledged projection, surface the limit locally and correct collection/metadata selection or coordinate another protocol extension. Retrying an oversized body unchanged is not useful.

V2 has separate quotas: **30 requests/minute/source IP** before authentication and **6 requests/minute/authenticated instance**. Its uploads/errors/retries do not consume v1's heartbeat/monitoring quota. Credentials, revocation, instance identity and the instance-row lock remain shared. Heartbeat processing and timestamp fields are independent; v2 does not freshen or change them. A recommended v2 cadence is one full snapshot per 60 seconds, with bounded retries, backoff/jitter and coalescing outside HA's event loop.

### Complete request example

```json
{
    "schema_version": 2,
    "instance_id": "7e9dcd76-5fb2-4ee1-8ee8-316f6d498fc1",
    "sent_at": "2026-11-07T10:00:01Z",
    "snapshots": [
        {
            "snapshot_id": "91ec4ab0-6176-416b-a4ea-49fac35ee443",
            "observed_at": "2026-11-07T10:00:00.123456Z",
            "metrics": {
                "cpu_percent": {
                    "status": "ok",
                    "value": 12.5,
                    "scope": "host"
                },
                "memory_percent": {
                    "status": "ok",
                    "value": 48,
                    "scope": "host"
                },
                "storage_percent": {
                    "status": "unsupported",
                    "value": null,
                    "scope": null
                }
            },
            "inventory": {
                "entities": { "status": "ok", "value": 1250 },
                "devices": { "status": "ok", "value": 80 },
                "automations": { "status": "ok", "value": 0 },
                "scenes": { "status": "unknown", "value": null },
                "integrations": { "status": "ok", "value": 35 }
            },
            "updates": {
                "coverage": "partial",
                "scope": "all",
                "counts": {
                    "total": 2,
                    "active": 2,
                    "disabled": 0,
                    "unknown": 0,
                    "unavailable": 1,
                    "available": 1,
                    "skipped": 0
                },
                "items": [
                    {
                        "key": "update.shelly_firmware",
                        "name": "Shelly-Firmware",
                        "category": "firmware",
                        "availability": "available",
                        "source": "shelly",
                        "device": {
                            "id": "a9b3e26a43a44a23921f893761409836",
                            "name": "Flur",
                            "manufacturer": "Shelly",
                            "model": "Plus 1PM"
                        },
                        "supports_install": false,
                        "installed_version": "20241011-123000/v1.14.1-g1234567",
                        "available_version": "20261101-160000/v1.15.0-gabcdef0",
                        "skipped_version": null,
                        "in_progress": false,
                        "disabled_by": null,
                        "required": true
                    },
                    {
                        "key": "update.immich",
                        "name": "Immich",
                        "category": "other",
                        "availability": "unavailable",
                        "source": "immich",
                        "device": null,
                        "supports_install": null,
                        "installed_version": null,
                        "available_version": null,
                        "skipped_version": null,
                        "in_progress": null,
                        "disabled_by": null,
                        "required": true
                    }
                ]
            }
        }
    ]
}
```

The envelope has exactly `schema_version`, `instance_id`, `sent_at`, `snapshots`. Each snapshot has exactly `snapshot_id`, `observed_at`, `metrics`, `inventory`, `updates`. All are required. Metrics/inventory keys, definitions, scopes, availability/value combinations and bounds are **identical to v1**. Sent/observation times use v1's RFC3339 rules, maximum six fractional digits, 24-hour replay window, five-minute future tolerance and `observed_at <= sent_at`. The JSON integer version must be exactly 2. Unknown fields at every level are rejected; optional device omission is the only omitted item metadata allowed.

### Coverage, export scope and counters

`updates` must have exactly `coverage`, `scope`, `counts`, `items`:

| Field/value             | Meaning                                                                                                                                                                                                                                    |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `scope: all`            | All sources discovered for export by the sender, including hidden active sources and records explaining disabled/missing sources. This is not a promise that HA exposes every device's possible updates.                                   |
| `scope: selected`       | Explicitly selected source set, including requested Core/OS sources. Completeness is relative to that set, never to the installation as a whole.                                                                                           |
| `coverage: complete`    | Every required source in this scope was reliably classified. No unknown/unavailable records or required disabled sources are allowed. Empty complete inventory is a reliable zero within the declared scope.                               |
| `coverage: partial`     | Known records are preserved, but failed/missing/required disabled sources, discovery failures or source-cap exhaustion prevent completeness. Empty partial collections are allowed. Offered counts are **lower bounds**, not exact totals. |
| `coverage: unknown`     | No reliable source inventory could be collected. Items must be empty and every counter zero; these zero counters are not an update-availability statement. Use partial when known records exist.                                           |
| `coverage: unsupported` | Collection is unsupported/unselected as a whole. Items must be empty and every counter zero; this never means up to date.                                                                                                                  |

All seven `counts` fields are required strict JSON integers from 0 through 4,096, and must equal the receiver-computable counts of **represented records**:

| Counter       | Definition                                                                                           |
| ------------- | ---------------------------------------------------------------------------------------------------- |
| `total`       | Number of items, including disabled records                                                          |
| `active`      | Items whose availability is not disabled                                                             |
| `disabled`    | Items with availability disabled, whether excluded or required                                       |
| `unknown`     | Items with availability unknown                                                                      |
| `unavailable` | Items with availability unavailable                                                                  |
| `available`   | Items with availability available, including non-installable offers and offers with unknown versions |
| `skipped`     | Items with availability skipped; these are still offered updates                                     |

`total = active + disabled`. The remaining active records after subtracting available/skipped/unknown/unavailable are up to date. **Known offered updates = available + skipped**. Do not count stale `skipped_version` metadata on an up-to-date record as an offered update. Do not add inaccessible/undiscovered sources to guessed totals. A partial record list can have internally consistent counters and still omit an unknown number of sources; coverage carries that uncertainty. The UI labels such counts “Mindestens …” and shows the export scope. A failed selected source must not block known updates from other sources, but it must prevent a complete claim.

Automatically discovered disabled sources may have `required: false` in all scope and are excluded from the active completeness requirement. Explicitly selected/requested sources have `required: true`; disabled selected Core/OS or other requested sources require partial coverage. In selected scope every record must be required. Unknown/unavailable records always prevent complete coverage. The sender must not enable sources or reuse an old on-state for a disabled record.

### Source records and original metadata

Each item has exactly the following keys. All keys are required except `device`, which may be omitted or null; both normalize to null for hashing/storage. Each source is unique by `key`; at most one core and one os record may appear in a collection, including their non-offered states.

| Field               | Type and bounds                                                                                                                                                                                                                                                                      |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `key`               | Stable source identifier, 1–256 lowercase ASCII characters matching `[a-z0-9][a-z0-9_.-]*`; e.g. an update entity ID                                                                                                                                                                 |
| `name`              | Required display text, 1–256 Unicode characters                                                                                                                                                                                                                                      |
| `category`          | `core`, `os`, `supervisor`, `addon`, `hacs`, `firmware`, `other`                                                                                                                                                                                                                     |
| `availability`      | `available`, `skipped`, `up_to_date`, `disabled`, `unknown`, `unavailable`                                                                                                                                                                                                           |
| `source`            | Null or integration domain: 1–64 ASCII characters matching `[a-z][a-z0-9_]*`                                                                                                                                                                                                         |
| `device`            | Omitted/null or exactly `{id, name, manufacturer, model}`. ID is required 1–128 ASCII characters matching `[A-Za-z0-9_-]+`; each other field is null or 1–256 Unicode characters. Use an instance-local opaque device registry ID, never MAC/IP/serial numbers/hardware identifiers. |
| `supports_install`  | Strict JSON boolean or null; true means the source exposes an install function, not authorization, reachability or guaranteed success. False means manual/external installation; null means unknown. **Never an availability filter.**                                               |
| `installed_version` | Null or original 1–256-character Unicode version text                                                                                                                                                                                                                                |
| `available_version` | Null or original 1–256-character Unicode version text, even when availability is available                                                                                                                                                                                           |
| `skipped_version`   | Null or original 1–256-character Unicode text; preserved independently of the current availability                                                                                                                                                                                   |
| `in_progress`       | Null, a strict JSON boolean, or a JSON number from 0 through 100 inclusive. True = installing, false = no active installation reported, number = reported percentage (including zero), null = unknown. No numeric strings.                                                           |
| `disabled_by`       | Null, `user`, or `integration`. Non-null is permitted only with availability disabled; the reason may be unknown/null on a disabled record.                                                                                                                                          |
| `required`          | Strict JSON boolean. False is permitted only for disabled records in all scope.                                                                                                                                                                                                      |

Unicode text fields reject control characters (Cc), format characters (Cf) and line/paragraph separators (Zl/Zp). Empty strings are rejected; use null for unknown optional text. Slash, colon, spaces, vendor punctuation, non-ASCII characters and version prefixes/suffixes are permitted. The receiver does **not** trim, truncate, normalize Unicode, rewrite versions, infer Semver or compare versions to decide availability. Whitespace in a nonempty identifier is preserved, including leading/trailing spaces. JSON booleans/numbers are not coerced to strings. Invalid UTF-8/JSON fails at transport validation. Free text is escaped by React, not interpreted as HTML or a link.

Availability is the sender's explicit HA observation. Available with null new version is still offered, with incomplete descriptive metadata. Skipped remains offered and is visibly marked. Up to date is not offered, even if old skip/version metadata remains. Disabled/unknown/unavailable are source states and never interpreted as stale available updates. An installation failure or absence of INSTALL must not erase an offered record. No install/remote-control endpoint or button is introduced.

Only the allowlisted metadata may leave HA. No config entries, release URLs/notes, arbitrary attributes, credentials, MAC/IP/serial numbers or full registries. Names and raw versions can still be sensitive; senders must limit them to the agreed explicit scope and keep raw metadata out of diagnostics/logs. Laravel's existing per-instance ACL controls current and historical props, including device metadata.

### Atomic acknowledgment, retries and mixed-version behavior

HTTP 200 returns `{ "accepted": 1, "duplicates": 0, "received_at": "2026-11-07T10:00:01+00:00" }` only after the whole collection, receipt, metric sample and applicable state transition commit to PostgreSQL. Matching retries return accepted 0/duplicates 1. No chunk is acknowledged and no Redis-only handoff is accepted. Any invalid last item leaves the previous projection and all history/receipts unchanged.

The per-instance receipt ledger, UUID lifetime and re-pair semantics are shared with v1. Generate a fresh UUID per new observation, **also across protocol versions**. V2 receipt hashes include schema version 2, normalized UUID/time, sorted object keys, normalized optional device null and all source metadata/counters/coverage/scope. Core and OS records sort first, followed by every other category by stable key. Input item/object key ordering and equivalent timezone/fractional timestamp forms do not change content identity. Altering coverage, version/device/install/skip metadata or any other content under a retained UUID returns 409. No raw full-payload copy is added to receipts.

Both versions project into the same current instance state, with the incoming update shape retained. The strictly newer observation wins regardless of protocol version; an equal/late observation retains its metric sample/receipt without regressing state. A new v1 snapshot remains a v1 full replacement and may remove v2 metadata; it is never silently upgraded or enriched. A new partial/unknown v2 collection also replaces the previous update state: Laravel does not resurrect old offers for missing sources. Known partial items come from that new observation; explicitly viewing older history is separate.

Every forward change to availability, metadata, coverage or counters is historized. Identical forward states add no repeated state-history entry. Skip → available → up to date is a real sequence of transitions; no backend version comparison substitutes for it. Re-pair clears current data while keeping receipt/history keys; replaying an acknowledged UUID cannot recreate current state. Credential revocation/deactivation and identity are rechecked under the same instance lock. Current/metric/history retention stays unchanged: samples/receipts 30 days, state transitions 90 days, bounded hourly cleanup; current state survives pruning. All metadata live in existing JSONB projections/history, so no schema migration is necessary.

The existing error classes apply: 400 malformed JSON/root/depth; 401 invalid/revoked/disabled machine; 403 missing HTTPS or identity mismatch; 409 conflicting ID; 413 bytes exceeded; 415 content type/encoding; 422 invalid schema/counts/records/coverage; 429 quota with Retry-After; 5xx/timeout bounded retry with frozen UUID/content. Correct permanent errors instead of retrying unchanged. Never fall back to independent v1 fragments for a failed v2 upload.

### Browser projection and verification

Cockpit/detail share the same v1/v2 presentation. Offered records include available and skipped regardless of INSTALL. Core/OS retain priority; the remaining seven-category metadata, device/source fields, raw versions, install progress and exclusion reasons remain visible. Non-offered source states are expandable. Lists initially render 50 entries and explicitly say “n von m”; further entries can be loaded without changing the stored collection or its coverage. Partial lower bounds remain visible in summary cards, update lists and history. A complete zero is distinguished from unknown/unsupported/partial zero. Existing instance ACLs and ten-second partial refresh remain authoritative.

```bash
vendor/bin/sail artisan test --compact tests/Feature/AgentMonitoringV2Test.php tests/Feature/AgentMonitoringTest.php tests/Feature/AgentProtocolTest.php
vendor/bin/sail npm run test:monitoring
```

These are Laravel-side fixtures, not verification of a real Shelly installation or of agent 0.2.3 sending v2. The separate sender must implement this exact contract, privacy/selection mapping and HTTPS end-to-end tests before rollout.
