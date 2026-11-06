"""Real HA fixtures and a strict in-process HTTPS Laravel stand-in."""

import asyncio
from collections import deque
from datetime import timedelta
import math
import re
import ssl
from unittest.mock import patch
from uuid import UUID, uuid4

from aiohttp import ClientSession, TCPConnector, web
from aiohttp.test_utils import TestServer
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant.common import MockConfigEntry
import trustme

from custom_components.ha_monitor_agent.const import DOMAIN


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
        """Validate the full Snapshot v1 schema independently of ingest."""
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
        assert len(raw) <= 65536
        if (
            self.revoked
            or request.headers.get("Authorization") != f"Bearer {self.token}"
        ):
            return web.json_response({}, status=401)
        assert set(payload) == {"schema_version", "instance_id", "sent_at", "snapshots"}
        assert type(payload["schema_version"]) is int and payload["schema_version"] == 1
        assert payload["instance_id"] == self.instance_id
        sent = dt_util.parse_datetime(payload["sent_at"])
        assert sent is not None and sent.tzinfo is not None
        assert sent <= dt_util.utcnow() + timedelta(minutes=5)
        assert 1 <= len(payload["snapshots"]) <= 20
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
    app = web.Application()
    app.router.add_post("/api/v1/agents/pair", backend.pair)
    app.router.add_post("/api/v1/agents/ingest", backend.ingest)
    app.router.add_post("/api/v1/agents/monitoring", backend.monitoring)
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
