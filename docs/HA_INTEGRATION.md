# Home Assistant Custom Integration - Design and Distribution

Status: Sprint 3 heartbeat integration implemented in this repository. See `README.md` for installation, supported versions, behavior and validation. The following is the original design brief; future data exports remain out of scope for protocol v1.

The Laravel-side Sprint 2 receiver is now implemented; Python development follows in a separate repository using the heartbeat-only v1 contract in PROTOCOL.md. Distribution through HACS requires a public repository; a custom repository is not the same as a private repository. Repository publication and installation will be decided for that milestone.

## Purpose

Build a proper Home Assistant custom integration, installable through a HACS-compatible repository or the Home Assistant Marketplace, to act as a lightweight outbound data agent for HA Monitor.

The integration gathers **selected, permitted monitoring data** from its local Home Assistant instance and sends it to the Laravel server. It does not require exposing the Home Assistant API or storing an HA administrator's Long-Lived Access Token in Laravel.

## Repository and packaging

Use an independent repository (proposed `ha-monitor-ha-integration`), with a standard layout:

```text
ha-monitor-ha-integration/
  custom_components/
    ha_monitor_agent/
      __init__.py
      manifest.json
      config_flow.py
      const.py
      coordinator.py        # only if justified
      client.py
      translations/
        en.json
        de.json             # when translated
  hacs.json
  README.md
  .github/workflows/      # HACS/Marketplace and hassfest checks
  tests/
```

This is a **proposed file layout**, not files currently present. Add files only where needed. HACS and the Marketplace require the integration code under `custom_components/<domain>/`; include a correctly versioned manifest and root `hacs.json`. For initial private deployment use a custom repository; catalog listing and public publication are separate decisions.

## Installation experience

1. Operator installs the repository through HACS (older HA) or Marketplace (HA 2026.11+) where supported.
2. Adds "HA Monitor" using **Settings > Devices & Services > Add Integration** via a config flow.
3. Enters the HTTPS monitoring server URL and short-lived pairing code created in Laravel.
4. Integration checks enrollment, stores the issued per-instance credential in its HA config entry, and reports success/failure.
5. Config and reauthentication flows allow reconnecting to a replacement receiver or rotating an invalid credential.
6. Options flow controls which _optional_ data categories/entities may be exported. Make defaults privacy-preserving.

No YAML-only installation should be required for the MVP. Do not build a large UI inside Home Assistant; its main purpose is pairing and sender configuration.

## Runtime behavior

- Use Home Assistant's async integration lifecycle (`async_setup_entry`, `async_unload_entry`) and config entries; cancel subscriptions/tasks on unload.
- For outgoing HTTP, use Home Assistant's shared async HTTP client/session helpers; do not perform blocking `requests` calls in the event loop.
- Start with v1 heartbeat, HA version and integration version only; inventory, snapshots and entity state export require a later explicitly versioned contract.
- For entity changes, use supported state-change helpers instead of scanning every entity continuously.
- Coalesce frequent updates and send bounded batches. Never create an unbounded per-change task or queue.
- Apply sensible timeouts, exponential backoff with jitter and a capped buffer. Avoid excessive CPU, memory or network traffic.
- Do not copy all HA entity attributes indiscriminately: they may contain sensitive URLs, identifiers, presence/location, alarm status, and other personal details.
- Preserve a reliable way to generate a fresh snapshot after connectivity returns or the integration restarts.
- Track integration version to aid compatibility checks with Laravel receiver API version.

## Minimum exported data (MVP proposal)

- Instance identity issued by the backend during pairing.
- Last-contact/heartbeat timestamp and integration version.
- HA core version and small optional technical counters where available through supported APIs.
- A small selected entity-state subset only after the user has approved collection rules.

Do not promise CPU/RAM/system metrics without checking whether the HA installation actually exposes them. Define those sources separately, rather than scraping unstable internals.

## Failure handling

| Condition                | Required behavior                                                                        |
| ------------------------ | ---------------------------------------------------------------------------------------- |
| Central receiver offline | Log a concise, rate-limited warning; retry later; HA stays usable                        |
| Timeout / 5xx            | Bounded backoff; no busy loop                                                            |
| 429                      | Respect Retry-After when valid; delay batch                                              |
| 400 / 413                | Do not retry invalid payload endlessly; surface diagnostics and shrink/split as designed |
| 401 / 403                | Stop useless retries; request reauthentication / re-pairing                              |
| HA restart               | Resume fresh v1 heartbeats; snapshot reconciliation requires a later protocol extension  |
| Credentials rotated      | Reauthentication flow; never print credentials                                           |

The initial implementation may lose individual transitions during a long offline period if it uses only memory. Document this honestly; a disk-backed queue is a **later requirement only if lossless history is actually needed**.

## HACS / Marketplace release checklist

- Ensure one integration under `custom_components/` and valid `manifest.json` with `domain`, `name`, `version`, `documentation`, `issue_tracker`, `codeowners`, `config_flow` as applicable.
- Provide `hacs.json`, a concise README, a public repository if publication is chosen, and versioned releases.
- Run Home Assistant `hassfest` and the HACS validation action before tagging releases.
- Test installation and upgrade from a custom repository, config flow, reauthentication, unload/reload, HA restart, and receiver downtime.
- Target the minimum HA version explicitly and test on supported versions; do not assume every new HA version remains compatible.

## Current HACS transition

Home Assistant 2026.11 integrates HACS functionality as **Marketplace**. Its publisher model still uses HACS's repository layout, `hacs.json`, catalog and validation tools; earlier Home Assistant versions retain the HACS custom integration. Design for both rather than tying the product to a UI label.

## Primary technical references

- HA custom integration manifest: https://contributors.home-assistant.io/core/integrations/manifest/
- HA config flows: https://contributors.home-assistant.io/core/integrations/config_flow/
- HA event helpers: https://contributors.home-assistant.io/core/integrations/events/
- HA Marketplace publishing: https://contributors.home-assistant.io/marketplace/guides/integrations/
- HA Marketplace repository validation: https://contributors.home-assistant.io/marketplace/reference/validation/
