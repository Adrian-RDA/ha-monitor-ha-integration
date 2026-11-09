"""Real HA fixtures and a strict in-process HTTPS Laravel stand-in."""

import asyncio
from collections import deque
from datetime import timedelta
import math
import re
import ssl
import unicodedata
from unittest.mock import patch
from uuid import UUID, uuid4

from aiohttp import ClientSession, TCPConnector, web
from aiohttp.test_utils import TestServer
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant.common import MockConfigEntry
import trustme

from custom_components.ha_monitor_agent.const import DOMAIN

from .health_backend import validate_health


def validate_updates_v2(updates):
    """Independent receiver-side contract checks; no integration validators used."""
    assert set(updates) == {"coverage", "scope", "counts", "items"}
    coverage, scope = updates["coverage"], updates["scope"]
    assert coverage in {"complete", "partial", "unknown", "unsupported"}
    assert scope in {"all", "selected"}
    items = updates["items"]
    assert len(items) <= 4096
    counts = updates["counts"]
    assert set(counts) == {
        "total",
        "active",
        "disabled",
        "unknown",
        "unavailable",
        "available",
        "skipped",
    }
    assert all(type(value) is int and 0 <= value <= 4096 for value in counts.values())
    assert counts["total"] == len(items)
    for state in ("disabled", "unknown", "unavailable", "available", "skipped"):
        assert counts[state] == sum(item["availability"] == state for item in items)
    assert counts["active"] == len(items) - counts["disabled"]
    if coverage in {"unknown", "unsupported"}:
        assert not items and not any(counts.values())

    def text(value, nullable=False):
        if nullable and value is None:
            return
        assert isinstance(value, str) and 1 <= len(value) <= 256
        assert not any(
            unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp", "Cs"}
            for char in value
        )

    keys, products = set(), set()
    for item in items:
        assert set(item) in (
            {
                "key",
                "name",
                "category",
                "availability",
                "source",
                "supports_install",
                "installed_version",
                "available_version",
                "skipped_version",
                "in_progress",
                "disabled_by",
                "required",
            },
            {
                "key",
                "name",
                "category",
                "availability",
                "source",
                "device",
                "supports_install",
                "installed_version",
                "available_version",
                "skipped_version",
                "in_progress",
                "disabled_by",
                "required",
            },
        )
        assert re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,255}", item["key"])
        assert item["key"] not in keys
        keys.add(item["key"])
        assert item["category"] in {
            "core",
            "os",
            "supervisor",
            "addon",
            "hacs",
            "firmware",
            "other",
        }
        if item["category"] in {"core", "os"}:
            assert item["category"] not in products
            products.add(item["category"])
        assert item["availability"] in {
            "available",
            "skipped",
            "up_to_date",
            "disabled",
            "unknown",
            "unavailable",
        }
        assert item["source"] is None or re.fullmatch(
            r"[a-z][a-z0-9_]{0,63}", item["source"]
        )
        text(item["name"])
        for field in ("installed_version", "available_version", "skipped_version"):
            text(item[field], nullable=True)
        assert (
            item["supports_install"] is None or type(item["supports_install"]) is bool
        )
        progress = item["in_progress"]
        assert (
            progress is None
            or type(progress) is bool
            or type(progress) in {int, float}
            and math.isfinite(progress)
            and 0 <= progress <= 100
        )
        assert item["disabled_by"] in {None, "user", "integration"}
        assert item["disabled_by"] is None or item["availability"] == "disabled"
        assert type(item["required"]) is bool
        assert item["required"] or scope == "all" and item["availability"] == "disabled"
        if coverage == "complete":
            assert item["availability"] not in {"unknown", "unavailable"}
            assert item["availability"] != "disabled" or not item["required"]
        device = item.get("device")
        if device is not None:
            assert set(device) == {"id", "name", "manufacturer", "model"}
            assert re.fullmatch(r"[A-Za-z0-9_-]{1,128}", device["id"])
            for field in ("name", "manufacturer", "model"):
                text(device[field], nullable=True)


class LaravelBackend:
    """Simulate v1 validation, one-time pairing, revocation and deduplication."""

    def __init__(self):
        self.instance_id = str(uuid4())
        self.code = uuid4().hex
        self.token = uuid4().hex + uuid4().hex
        self.paired = False
        self.revoked = False
        self.pair_calls = 0
        self.pair_requests = []
        self.requests = []
        self.stored = {}
        self.responses = deque()
        self.commit_then_fail = False
        self.gate = None
        self.request_started = asyncio.Event()
        self.request_finished = asyncio.Event()
        self.expected_tls_rejections = 0
        # Exercise compatibility with the original single-heartbeat receiver,
        # independently of the upper bound advertised by the v1 pair response.
        self.max_ingest_events = 1
        self.monitoring_requests = []
        self.monitoring_stored = {}
        self.monitoring_responses = deque()
        self.monitoring_commit_then_fail = False
        self.monitoring_gate = None
        self.monitoring_started = asyncio.Event()
        self.health_requests = []
        self.health_stored = {}
        self.health_responses = deque()
        self.health_commit_then_fail = False
        self.health_gate = None
        self.health_started = asyncio.Event()
        self.limits = {
            "heartbeat_interval_seconds": 60,
            "max_batch_events": 100,
            "max_body_bytes": 65536,
        }

    @property
    def pairing(self):
        return {
            "schema_version": 1,
            "instance_id": self.instance_id,
            "agent_token": self.token,
            **self.limits,
        }

    def new_code(self):
        self.code = uuid4().hex
        self.revoked = True
        self.paired = False
        self.token = uuid4().hex + uuid4().hex

    async def pair(self, request):
        self.pair_calls += 1
        raw = await request.read()
        data = await request.json()
        self.pair_requests.append((data, dict(request.headers), len(raw)))
        assert request.secure
        assert set(data) == {"code"}
        if data["code"] != self.code or self.paired:
            return web.json_response({}, status=401)
        self.paired = True
        self.revoked = False
        return web.json_response(self.pairing)

    async def ingest(self, request):
        raw = await request.read()
        payload = await request.json()
        self.requests.append((payload, dict(request.headers), len(raw)))
        self.request_started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.responses:
            return self.responses.popleft()
        if (
            self.revoked
            or request.headers.get("Authorization") != f"Bearer {self.token}"
        ):
            return web.json_response({}, status=401)
        if payload.get("instance_id") != self.instance_id:
            return web.json_response({}, status=403)
        assert request.secure
        assert request.content_type == "application/json"
        assert "Content-Encoding" not in request.headers
        assert len(raw) <= self.limits["max_body_bytes"]
        assert set(payload) == {
            "schema_version",
            "instance_id",
            "integration_version",
            "sent_at",
            "events",
        }
        assert type(payload["schema_version"]) is int and payload["schema_version"] == 1
        assert str(UUID(payload["instance_id"])) == self.instance_id
        assert re.fullmatch(
            r"[0-9][A-Za-z0-9.+_-]{0,31}", payload["integration_version"]
        )
        sent_at = dt_util.parse_datetime(payload["sent_at"])
        assert sent_at is not None and sent_at.tzinfo is not None
        assert dt_util.utcnow() - timedelta(hours=24) <= sent_at
        assert 1 <= len(payload["events"]) <= self.limits["max_batch_events"]
        if len(payload["events"]) > self.max_ingest_events:
            return web.json_response({}, status=422)
        assert len({event["event_id"] for event in payload["events"]}) == len(
            payload["events"]
        )
        accepted = duplicates = 0
        for event in payload["events"]:
            assert set(event) == {"event_id", "type", "occurred_at", "data"}
            assert str(UUID(event["event_id"])) == event["event_id"]
            assert event["type"] == "heartbeat"
            assert set(event["data"]) == {"ha_version"}
            assert re.fullmatch(
                r"[0-9][A-Za-z0-9.+_-]{0,31}", event["data"]["ha_version"]
            )
            occurred_at = dt_util.parse_datetime(event["occurred_at"])
            assert occurred_at is not None and occurred_at.tzinfo is not None
            assert dt_util.utcnow() - timedelta(hours=24) <= occurred_at <= sent_at
            assert sent_at <= dt_util.utcnow() + timedelta(minutes=5)
            if event["event_id"] in self.stored:
                duplicates += 1
            else:
                self.stored[event["event_id"]] = event
                accepted += 1
        if self.commit_then_fail:
            self.commit_then_fail = False
            return web.json_response({}, status=503)
        self.request_finished.set()
        return web.json_response(
            {
                "accepted": accepted,
                "duplicates": duplicates,
                "received_at": dt_util.utcnow().isoformat(),
            }
        )

    async def monitoring(self, request):
        """Validate both snapshot contracts independently of ingest and senders."""
        version = 2 if request.path == "/api/v2/agents/monitoring" else 1
        raw = await request.read()
        payload = await request.json()
        self.monitoring_requests.append((payload, dict(request.headers), len(raw)))
        self.monitoring_started.set()
        if self.monitoring_gate is not None:
            await self.monitoring_gate.wait()
        if self.monitoring_responses:
            return self.monitoring_responses.popleft()
        assert request.secure and request.content_type == "application/json"
        assert "Content-Encoding" not in request.headers
        assert len(raw) <= (2_097_152 if version == 2 else 65536)
        if (
            self.revoked
            or request.headers.get("Authorization") != f"Bearer {self.token}"
        ):
            return web.json_response({}, status=401)
        assert set(payload) == {"schema_version", "instance_id", "sent_at", "snapshots"}
        assert (
            type(payload["schema_version"]) is int
            and payload["schema_version"] == version
        )
        assert payload["instance_id"] == self.instance_id
        sent = dt_util.parse_datetime(payload["sent_at"])
        assert sent is not None and sent.tzinfo is not None
        assert sent <= dt_util.utcnow() + timedelta(minutes=5)
        assert 1 <= len(payload["snapshots"]) <= (1 if version == 2 else 20)
        ids = set()
        for snapshot in payload["snapshots"]:
            assert set(snapshot) == {
                "snapshot_id",
                "observed_at",
                "metrics",
                "inventory",
                "updates",
            }
            key = snapshot["snapshot_id"]
            assert str(UUID(key)) == key and key not in ids
            ids.add(key)
            observed = dt_util.parse_datetime(snapshot["observed_at"])
            assert observed is not None and observed.tzinfo is not None
            assert dt_util.utcnow() - timedelta(hours=24) <= observed <= sent
            assert set(snapshot["metrics"]) == {
                "cpu_percent",
                "memory_percent",
                "storage_percent",
            }
            assert set(snapshot["inventory"]) == {
                "entities",
                "devices",
                "automations",
                "scenes",
                "integrations",
            }
            for section in ("metrics", "inventory"):
                for name, value in snapshot[section].items():
                    assert set(value) == (
                        {"status", "value", "scope"}
                        if section == "metrics"
                        else {"status", "value"}
                    )
                    assert value["status"] in {
                        "ok",
                        "unknown",
                        "unavailable",
                        "unsupported",
                    }
                    if value["status"] == "ok":
                        assert type(value["value"]) in (int, float)
                        assert math.isfinite(value["value"])
                        assert (
                            0
                            <= value["value"]
                            <= (100 if section == "metrics" else 1_000_000)
                        )
                        if section == "inventory":
                            assert type(value["value"]) is int
                    else:
                        assert value["value"] is None
                    if section == "metrics":
                        allowed = (
                            {"host", "data"}
                            if name == "storage_percent"
                            else {"host", "home_assistant"}
                        )
                        assert value["scope"] in allowed | {None}
                        assert value["status"] != "ok" or value["scope"] is not None
            updates = snapshot["updates"]
            if version == 2:
                validate_updates_v2(updates)
                if (
                    key in self.monitoring_stored
                    and self.monitoring_stored[key] != snapshot
                ):
                    return web.json_response({}, status=409)
                continue
            assert set(updates) == {"status", "items"}
            assert updates["status"] in {"ok", "unknown", "unavailable", "unsupported"}
            assert len(updates["items"]) <= 100
            assert updates["status"] == "ok" or not updates["items"]
            item_keys, products = set(), set()
            for item in updates["items"]:
                assert set(item) == {
                    "key",
                    "kind",
                    "name",
                    "installed_version",
                    "available_version",
                }
                assert re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", item["key"])
                assert item["key"] not in item_keys
                item_keys.add(item["key"])
                assert item["kind"] in {"core", "os", "other"}
                if item["kind"] != "other":
                    assert item["kind"] not in products
                    products.add(item["kind"])
                assert 1 <= len(item["name"]) <= 120
                assert not any(ord(char) < 32 for char in item["name"])
                for version_key in ("installed_version", "available_version"):
                    version = item[version_key]
                    if version_key == "installed_version" and version is None:
                        continue
                    assert re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}", version)
            if (
                key in self.monitoring_stored
                and self.monitoring_stored[key] != snapshot
            ):
                return web.json_response({}, status=409)
        accepted = duplicates = 0
        for snapshot in payload["snapshots"]:
            key = snapshot["snapshot_id"]
            if key in self.monitoring_stored:
                duplicates += 1
            else:
                self.monitoring_stored[key] = snapshot
                accepted += 1
        if self.monitoring_commit_then_fail:
            self.monitoring_commit_then_fail = False
            return web.json_response({}, status=503)
        return web.json_response(
            {
                "accepted": accepted,
                "duplicates": duplicates,
                "received_at": dt_util.utcnow().isoformat(),
            }
        )

    async def health(self, request):
        raw = await request.read()
        payload = await request.json()
        self.health_requests.append((payload, dict(request.headers), len(raw)))
        self.health_started.set()
        if self.health_gate is not None:
            await self.health_gate.wait()
        assert request.secure and request.content_type == "application/json"
        assert len(raw) <= 262144
        assert request.headers.get("Content-Encoding") is None
        assert request.headers["Authorization"] == f"Bearer {self.token}"
        if self.revoked:
            return web.json_response({}, status=401)
        assert payload["instance_id"] == self.instance_id
        validate_health(payload)
        if self.health_responses:
            status, headers = self.health_responses.popleft()
            return web.json_response({}, status=status, headers=headers)
        report = payload["report"]
        key = report["report_id"]
        duplicate = key in self.health_stored
        if duplicate and self.health_stored[key] != report:
            return web.json_response({}, status=409)
        self.health_stored[key] = report
        if self.health_commit_then_fail:
            self.health_commit_then_fail = False
            return web.json_response({}, status=503)
        return web.json_response(
            {
                "accepted": int(not duplicate),
                "duplicates": int(duplicate),
                "received_at": dt_util.utcnow().isoformat(),
            }
        )


@pytest.fixture
async def backend(hass, socket_enabled):
    """TLS is fully verified against a private CA created for each test only."""
    backend = LaravelBackend()
    ca = trustme.CA()
    cert = ca.issue_cert("127.0.0.1")
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    cert.configure_cert(server_context)
    client_context = ssl.create_default_context()
    ca.configure_trust(client_context)
    app = web.Application(client_max_size=2_097_153)
    app.router.add_post("/api/v1/agents/pair", backend.pair)
    app.router.add_post("/api/v1/agents/ingest", backend.ingest)
    app.router.add_post("/api/v1/agents/monitoring", backend.monitoring)
    app.router.add_post("/api/v2/agents/monitoring", backend.monitoring)
    app.router.add_post("/api/v1/agents/health", backend.health)
    server = TestServer(app, scheme="https")
    await server.start_server(ssl=server_context)
    backend.url = str(server.make_url("/")).rstrip("/")
    loop = asyncio.get_running_loop()
    original_exception_handler = loop.get_exception_handler()

    def handle_test_server_error(loop, context):
        # Rejecting the test CA can reset the server-side TLS handshake. This is
        # expected only in tests that explicitly opt in; all other errors fail.
        if (
            backend.expected_tls_rejections
            and isinstance(context.get("exception"), ConnectionResetError)
            and context.get("message")
            == "Error on transport creation for incoming connection"
        ):
            backend.expected_tls_rejections -= 1
            return
        original_exception_handler(loop, context)

    loop.set_exception_handler(handle_test_server_error)
    async with ClientSession(connector=TCPConnector(ssl=client_context)) as session:
        backend.session = session
        with (
            patch(
                "custom_components.ha_monitor_agent.async_get_clientsession",
                return_value=session,
            ),
            patch(
                "custom_components.ha_monitor_agent.config_flow.async_get_clientsession",
                return_value=session,
            ),
        ):
            yield backend
        # Shut down entry-scoped workers before closing the injected shared session.
        for entry in hass.config_entries.async_entries(DOMAIN):
            if hasattr(entry, "runtime_data"):
                await entry.runtime_data.async_stop()
        if backend.gate is not None:
            backend.gate.set()
        if backend.monitoring_gate is not None:
            backend.monitoring_gate.set()
        if backend.health_gate is not None:
            backend.health_gate.set()
    await server.close()
    loop.set_exception_handler(original_exception_handler)


@pytest.fixture
def entry(hass, backend):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="HA Monitor test",
        unique_id=backend.instance_id,
        data={
            "server_url": backend.url,
            **{k: v for k, v in backend.pairing.items() if k != "schema_version"},
        },
    )
    entry.add_to_hass(hass)
    return entry
