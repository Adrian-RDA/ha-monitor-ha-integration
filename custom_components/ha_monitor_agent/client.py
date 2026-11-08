"""Small, HA-independent async client for the Laravel agent contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import json
import re
from typing import Any
from uuid import UUID

from aiohttp import ClientError, ClientSession, ClientTimeout
from aiohttp.client_exceptions import ClientSSLError
from yarl import URL

from .const import (
    MAX_BODY_BYTES,
    MAX_BUFFER_EVENTS,
    MAX_RESPONSE_BYTES,
    MAX_RETRY_AFTER,
    REQUEST_TIMEOUT,
    SCHEMA_VERSION,
)


class AgentError(Exception):
    """A safe error code, never a response body, URL, or credential."""


class AuthenticationError(AgentError):
    """The credential or pairing code is no longer valid."""


class TransportError(AgentError):
    """A temporary connectivity or receiver failure."""

    def __init__(self, code: str, retry_after: float | None = None) -> None:
        super().__init__(code)
        self.retry_after = retry_after


class ProtocolError(AgentError):
    """A permanent failure requiring correction, without unchanged retries."""


def normalize_server_url(value: str) -> str:
    """Accept a verified HTTPS base URL, optionally including a path prefix."""
    try:
        url = URL(value.strip())
        if (
            url.scheme != "https"
            or not url.host
            or url.user is not None
            or url.password is not None
            or url.query_string
            or url.fragment
            or url.port is None
        ):
            raise ValueError
        # Reject hidden control characters rather than silently normalizing them.
        if any(ord(char) < 33 for char in value.strip()):
            raise ValueError
        return str(url).rstrip("/")
    except (ValueError, TypeError) as err:
        raise ProtocolError("invalid_url") from err


def _positive_int(data: dict[str, Any], key: str, upper: int) -> int:
    value = data.get(key)
    if type(value) is not int or not 1 <= value <= upper:
        raise ProtocolError("invalid_response")
    return value


def parse_timestamp(value: Any) -> datetime:
    """Require an explicitly timezone-aware RFC3339 timestamp."""
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
        r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
        value,
    ):
        raise ProtocolError("invalid_response")
    try:
        return datetime.fromisoformat(value).astimezone(UTC)
    except (ValueError, OverflowError) as err:
        raise ProtocolError("invalid_response") from err


@dataclass(frozen=True)
class Pairing:
    """Validated enrollment result. Exclude secrets from repr."""

    instance_id: str
    agent_token: str = field(repr=False)
    heartbeat_interval_seconds: int
    max_batch_events: int
    max_body_bytes: int


def encode_payload(payload: dict[str, Any]) -> bytes:
    """Use the exact same encoding for size checks and transport."""
    return json.dumps(
        payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip(" \t")
    # RFC 9110 delay-seconds is ASCII digits, not Python's broader int syntax.
    if re.fullmatch(r"[0-9]+", value):
        digits = value.lstrip("0") or "0"
        if len(digits) > len(str(MAX_RETRY_AFTER)):
            raise ProtocolError("retry_after_too_long")
        delay = int(digits)
    else:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            delay = max(0.0, (date - datetime.now(UTC)).total_seconds())
        except ValueError, TypeError, OverflowError:
            return None
    if delay > MAX_RETRY_AFTER:
        # Do not retry earlier than requested, nor keep an unbounded timer.
        raise ProtocolError("retry_after_too_long")
    return float(delay)


class AgentClient:
    """Use an injected HA session; never own or close the shared session."""

    def __init__(self, session: ClientSession, server_url: str) -> None:
        self._session = session
        self.server_url = normalize_server_url(server_url)

    async def async_pair(self, code: str) -> Pairing:
        """Redeem a one-time code exactly once; no automatic pairing retries."""
        if not re.fullmatch(r"[0-9a-f]{32}", code):
            raise ProtocolError("invalid_code")
        data = await self._async_post("pair", encode_payload({"code": code}))
        token, instance_id = data.get("agent_token"), data.get("instance_id")
        if (
            type(data.get("schema_version")) is not int
            or data["schema_version"] != SCHEMA_VERSION
            or not isinstance(token, str)
            or not re.fullmatch(r"[0-9a-f]{64}", token)
            or not isinstance(instance_id, str)
        ):
            raise ProtocolError("invalid_response")
        try:
            instance_id = str(UUID(instance_id))
        except ValueError as err:
            raise ProtocolError("invalid_response") from err
        return Pairing(
            instance_id=instance_id,
            agent_token=token,
            heartbeat_interval_seconds=_positive_int(
                data, "heartbeat_interval_seconds", 86_400
            ),
            max_batch_events=_positive_int(data, "max_batch_events", MAX_BUFFER_EVENTS),
            max_body_bytes=_positive_int(data, "max_body_bytes", MAX_BODY_BYTES),
        )

    async def async_ingest(self, body: bytes, token: str, count: int) -> datetime:
        """Only acknowledge a complete, valid, durably committed batch."""
        return await self._async_acknowledge("ingest", body, token, count)

    async def async_monitoring(self, body: bytes, token: str, count: int) -> datetime:
        """Send an atomic Snapshot v2 collection; pairing/heartbeats remain v1."""
        return await self._async_acknowledge(
            "monitoring", body, token, count, api_version=2
        )

    async def _async_acknowledge(
        self,
        endpoint: str,
        body: bytes,
        token: str,
        count: int,
        *,
        api_version: int = 1,
    ) -> datetime:
        data = await self._async_post(endpoint, body, token, api_version=api_version)
        accepted, duplicates = data.get("accepted"), data.get("duplicates")
        if (
            type(accepted) is not int
            or type(duplicates) is not int
            or accepted < 0
            or duplicates < 0
            or accepted + duplicates != count
        ):
            raise ProtocolError("invalid_response")
        return parse_timestamp(data.get("received_at"))

    async def _async_post(
        self,
        endpoint: str,
        body: bytes,
        token: str | None = None,
        *,
        api_version: int = 1,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with self._session.post(
                f"{self.server_url}/api/v{api_version}/agents/{endpoint}",
                data=body,
                headers=headers,
                timeout=ClientTimeout(total=REQUEST_TIMEOUT),
                allow_redirects=False,
            ) as response:
                if response.status == 401:
                    raise AuthenticationError("invalid_auth")
                if response.status == 403:
                    raise ProtocolError("forbidden")
                if response.status == 429 or 500 <= response.status < 600:
                    raise TransportError(
                        "rate_limited" if response.status == 429 else "server_error",
                        _retry_after(response.headers.get("Retry-After")),
                    )
                if response.status != 200:
                    raise ProtocolError(f"http_{response.status}")
                raw = bytearray()
                async for chunk in response.content.iter_chunked(4096):
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise ProtocolError("invalid_response")
                try:
                    data = json.loads(raw)
                except (ValueError, UnicodeError, RecursionError) as err:
                    raise ProtocolError("invalid_response") from err
                if not isinstance(data, dict):
                    raise ProtocolError("invalid_response")
                return data
        except ClientSSLError:
            raise ProtocolError("tls_error") from None
        except ClientError, TimeoutError:
            # aiohttp exceptions can contain credentials/URLs: never propagate them.
            raise TransportError("cannot_connect") from None
