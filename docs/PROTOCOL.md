# HA Monitor - Agent / Server Protocol v1

Status: **Laravel receiver implemented and tested in Sprint 2.** This is the contract for the subsequent Python integration repository. No Python sender is included here. `snapshot`, `entity_state` and automatic token rotation are not implemented.

## Transport and authentication

All agent requests require HTTPS, JSON objects, `Content-Type: application/json`, and uncompressed bodies. Plain HTTP returns 403, including on localhost; tests simulate HTTPS through Laravel's request boundary. Configure a valid TLS ingress and explicit trusted proxy addresses before a real installation. Never disable certificate validation or trust arbitrary forwarded headers.

These routes are stateless and independent of browser login, CSRF, Fortify and Spatie. Browser login never substitutes for an agent token. Credentials identify the instance; the request body cannot select another instance. No HA administrative token or inbound HA connection is used.

| Method | Endpoint                | Authentication                        |
| ------ | ----------------------- | ------------------------------------- |
| POST   | `/api/v1/agents/pair`   | One-time code in JSON                 |
| POST   | `/api/v1/agents/ingest` | `Authorization: Bearer <agent_token>` |

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
- Dashboard and detail status derive from timestamps at read time, avoiding stored status/scheduler races. Browser data refreshes every 30 seconds. The scheduler runs `monitoring:prune` hourly to remove expired code hashes and up to 10,000 events received more than 30 days ago per run. At high sustained volume cleanup can have a backlog; this is a bounded MVP housekeeping policy, not a hard deletion deadline. Current instance metadata remains available.
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

Sender buffers/retries must remain bounded and must not block HA. Durable replay across HA restarts, entity exports, snapshots, supported HA versions and reconciliation behavior are subsequent integration work. Do not claim exactly-once delivery.

## Local verification

```bash
vendor/bin/sail artisan test --compact tests/Feature/AgentProtocolTest.php tests/Feature/InstanceAccessTest.php tests/Feature/InstanceManagementTest.php
```

These tests use the separate PostgreSQL `testing` database and simulated HTTPS; no live HA integration or exposed receiver is required. Real HA-to-Laravel testing needs a separately configured valid HTTPS endpoint. Sail remains bound to loopback; no TLS bypass or infrastructure exposure was added.
