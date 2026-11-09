"""Independent Health v1 receiver validation for the simulated TLS backend."""

import re
from uuid import UUID

from homeassistant.util import dt as dt_util


def validate_health(payload):
    assert set(payload) == {"schema_version", "instance_id", "sent_at", "report"}
    assert type(payload["schema_version"]) is int and payload["schema_version"] == 1
    sent = dt_util.parse_datetime(payload["sent_at"])
    assert sent is not None and sent.tzinfo
    report = payload["report"]
    assert set(report) == {
        "report_id",
        "observed_at",
        "repairs",
        "config_entries",
        "unavailable_entities",
        "backup",
    }
    assert str(UUID(report["report_id"])) == report["report_id"]
    observed = dt_util.parse_datetime(report["observed_at"])
    assert observed is not None and observed.tzinfo and observed <= sent
    for section, cap in (
        ("repairs", 100),
        ("config_entries", 256),
        ("unavailable_entities", 1000),
    ):
        value = report[section]
        assert set(value) == {"coverage", "scope", "total", "items"}
        assert value["coverage"] in {
            "complete",
            "partial",
            "unknown",
            "unavailable",
            "unsupported",
        }
        assert value["scope"] in {"all", "selected"}
        total, items = value["total"], value["items"]
        assert isinstance(items, list) and len(items) <= cap
        assert total is None or type(total) is int and len(items) <= total <= 1_000_000
        if value["coverage"] == "complete":
            assert total == len(items)
        if value["coverage"] in {"unknown", "unavailable", "unsupported"}:
            assert total is None and not items
        keys = set()
        for item in items:
            assert item["name"] is None  # This sender never approves names.
            key = item["entity_id" if section == "unavailable_entities" else "key"]
            assert key not in keys
            keys.add(key)
            if section == "unavailable_entities":
                assert set(item) == {"entity_id", "name", "device"}
                assert len(key) <= 256 and re.fullmatch(
                    r"[a-z_][a-z0-9_]*\.[a-z0-9_]+", key
                )
                if item["device"] is not None:
                    assert set(item["device"]) == {"id", "name"}
                    assert item["device"]["name"] is None
                    assert re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", item["device"]["id"]
                    )
            else:
                assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", key)
                assert re.fullmatch(r"[a-z][a-z0-9_]{0,63}", item["domain"])
                if section == "repairs":
                    assert set(item) == {"key", "domain", "name", "status", "severity"}
                    assert item["status"] in {"open", "ignored"}
                    assert item["severity"] in {"critical", "error", "warning"}
                else:
                    assert set(item) == {"key", "domain", "name", "state"}
                    assert item["state"] in {
                        "setup_error",
                        "setup_retry",
                        "migration_error",
                        "failed_unload",
                        "not_loaded",
                        "setup_in_progress",
                        "unload_in_progress",
                    }
    backup = report["backup"]
    assert set(backup) == {
        "coverage",
        "status",
        "last_success_at",
        "last_attempt_at",
        "error_code",
    }
    assert backup["coverage"] in {"complete", "unknown", "unavailable", "unsupported"}
    assert backup["status"] in {"ok", "failed", "running", "never", "unknown"}
    success, attempt = [
        dt_util.parse_datetime(backup[key]) if backup[key] else None
        for key in ("last_success_at", "last_attempt_at")
    ]
    for value in (success, attempt):
        assert value is None or value.tzinfo and value <= observed
    assert (backup["status"] == "failed") == (backup["error_code"] is not None)
    assert backup["error_code"] in {
        None,
        "authentication",
        "connection",
        "storage",
        "insufficient_space",
        "timeout",
        "unknown",
    }
    if backup["coverage"] != "complete":
        assert (
            backup["status"] == "unknown"
            and success is attempt is backup["error_code"] is None
        )
    if backup["status"] in {"ok", "failed", "running"}:
        assert attempt is not None
    if backup["status"] == "ok":
        assert success is not None and success >= attempt
    elif success is not None:
        assert attempt is not None and success <= attempt
    if backup["status"] == "never":
        assert success is attempt is None
