"""Hostile/malformed responses, timeouts and verified HTTPS transport."""

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from unittest.mock import patch

from aiohttp import ClientConnectionError, ClientSession, web
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
import pytest

from custom_components.ha_monitor_agent.client import (
    AgentClient,
    AuthenticationError,
    ProtocolError,
    TransportError,
    normalize_server_url,
    parse_timestamp,
)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.org",
        "https://u:p@example.org",
        "https://example.org/?token=secret",
        "https://example.org/#secret",
        "https://example.org:bad",
        "https://example.org:99999",
        "https://exam\nple.org",
        "https:///",
        "",
        "ftp://example.org",
    ],
)
def test_reject_unsafe_url(url):
    with pytest.raises(ProtocolError, match="invalid_url"):
        normalize_server_url(url)


def test_path_prefix_normalized():
    assert (
        normalize_server_url(" https://EXAMPLE.org/monitor/ ")
        == "https://example.org/monitor"
    )


async def test_pairing_is_single_use_and_repr_safe(backend):
    client = AgentClient(backend.session, backend.url)
    pairing = await client.async_pair(backend.code)
    assert backend.token not in repr(pairing)
    payload, headers, size = backend.pair_requests[0]
    assert payload == {"code": backend.code}
    assert headers["Content-Type"] == "application/json"
    assert "Authorization" not in headers
    assert "Content-Encoding" not in headers
    assert size < 65536
    with pytest.raises(AuthenticationError):
        await client.async_pair(backend.code)
    assert backend.pair_calls == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("agent_token", "secret"),
        ("instance_id", "not-a-uuid"),
        ("instance_id", None),
        ("heartbeat_interval_seconds", 0),
        ("heartbeat_interval_seconds", 60.0),
        ("max_batch_events", 101),
        ("max_batch_events", True),
        ("max_body_bytes", 65537),
    ],
)
async def test_invalid_pair_response(backend, field, value):
    client = AgentClient(backend.session, backend.url)
    payload = {**backend.pairing, field: value}
    with patch.object(client, "_async_post", return_value=payload):
        with pytest.raises(ProtocolError, match="invalid_response"):
            await client.async_pair(backend.code)


@pytest.mark.parametrize(
    "body",
    [
        b"not JSON",
        b"[]",
        b"null",
        b"\xff",
        b"x" * 65537,
        b'{"accepted":true,"duplicates":0,"received_at":"2026-11-05T12:00:00Z"}',
        b'{"accepted":1,"duplicates":0,"received_at":"2026-11-05T12:00:00"}',
        b'{"accepted":1,"duplicates":0,"received_at":"2026-99-05T12:00:00Z"}',
    ],
)
async def test_invalid_ingest_responses(backend, body):
    backend.responses.append(web.Response(body=body, content_type="application/json"))
    client = AgentClient(backend.session, backend.url)
    with pytest.raises(ProtocolError, match="invalid_response"):
        await client.async_ingest(b"{}", backend.token, 1)


@pytest.mark.parametrize(
    ("header", "minimum", "maximum"),
    [
        ("120", 120, 120),
        ("0", 0, 0),
        ("-1", None, None),
        ("broken", None, None),
        ("1.5", None, None),
        ("+120", None, None),
        ("1_20", None, None),
        ("000120", 120, 120),
        ("date", 119, 121),
        ("Thu, 05 Nov 2026 12:00:00", None, None),
    ],
)
async def test_retry_after(backend, header, minimum, maximum):
    if header == "date":
        header = format_datetime(dt_util.utcnow() + timedelta(seconds=120), usegmt=True)
    backend.responses.append(web.Response(status=429, headers={"Retry-After": header}))
    with pytest.raises(TransportError) as error:
        await AgentClient(backend.session, backend.url).async_ingest(
            b"{}", backend.token, 1
        )
    if minimum is None:
        assert error.value.retry_after is None
    else:
        assert minimum <= error.value.retry_after <= maximum


@pytest.mark.parametrize("header", ["86401", "9" * 400, "9" * 5000])
async def test_excessive_retry_after_stops_without_early_retry(backend, header):
    backend.responses.append(web.Response(status=429, headers={"Retry-After": header}))
    with pytest.raises(ProtocolError, match="retry_after_too_long"):
        await AgentClient(backend.session, backend.url).async_ingest(
            b"{}", backend.token, 1
        )


@pytest.mark.parametrize(
    "error", [TimeoutError("secret"), ClientConnectionError("secret")]
)
async def test_transport_errors_are_safe_and_retryable(backend, error):
    with patch.object(backend.session, "post", side_effect=error):
        with pytest.raises(TransportError, match="cannot_connect") as result:
            await AgentClient(backend.session, backend.url).async_pair(backend.code)
    assert "secret" not in str(result.value)
    assert result.value.__suppress_context__


async def test_untrusted_certificate_rejected(backend):
    # The backend's test CA is deliberately absent from the default trust store.
    backend.expected_tls_rejections = 1
    async with ClientSession() as untrusted_session:
        with pytest.raises(ProtocolError, match="tls_error"):
            await AgentClient(untrusted_session, backend.url).async_pair(backend.code)
    assert backend.pair_calls == 0


async def test_shared_home_assistant_session_rejects_unknown_ca(hass, backend):
    # Test the actual production session helper too, without the fixture injection.
    backend.expected_tls_rejections = 1
    session = async_get_clientsession(hass)
    with pytest.raises(ProtocolError, match="tls_error"):
        await AgentClient(session, backend.url).async_pair(backend.code)
    assert not session.closed
    assert backend.pair_calls == 0


async def test_redirect_does_not_redeem_code_or_leak_token(backend):
    backend.responses.append(
        web.Response(
            status=307, headers={"Location": backend.url + "/api/v1/agents/pair"}
        )
    )
    with pytest.raises(ProtocolError, match="http_307"):
        await AgentClient(backend.session, backend.url).async_ingest(
            b"{}", backend.token, 1
        )
    assert backend.pair_calls == 0
    assert len(backend.requests) == 1


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-11-05T12:00:00",
        "2026-11-05T12:00:00.1234567Z",
        "2026-11-05T12:00:00+00:60",
        "2026-11-05T12:00:00+24:00",
        "9999-12-31T23:59:59-00:01",
        "0001-01-01T00:00:00+00:01",
    ],
)
def test_invalid_rfc3339_timestamps_fail_safely(timestamp):
    with pytest.raises(ProtocolError, match="invalid_response"):
        parse_timestamp(timestamp)


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-11-05T12:00:00.123456Z",
        "2026-11-05T13:00:00.123456+01:00",
        "2026-11-05T11:00:00.123456-01:00",
    ],
)
def test_timestamp_offsets_and_microseconds(timestamp):
    assert parse_timestamp(timestamp) == datetime(2026, 11, 5, 12, 0, 0, 123456, UTC)
