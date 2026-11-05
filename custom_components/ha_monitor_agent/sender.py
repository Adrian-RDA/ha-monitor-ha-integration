"""Bounded outbound heartbeat worker, independent of entity polling."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
import logging
import random
import re
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .client import (
    AgentClient,
    AuthenticationError,
    ProtocolError,
    TransportError,
    encode_payload,
)
from .const import (
    BACKOFF_BASE,
    BACKOFF_CAP,
    CONF_AGENT_TOKEN,
    CONF_ENABLED,
    CONF_HEARTBEAT_INTERVAL,
    CONF_INSTANCE_ID,
    CONF_MAX_BATCH_EVENTS,
    CONF_MAX_BODY_BYTES,
    DOMAIN,
    MAX_BUFFER_EVENTS,
    SCHEMA_VERSION,
    VERSION,
)

if TYPE_CHECKING:
    from . import AgentConfigEntry

_LOGGER = logging.getLogger(__name__)
STATUSES = ["online", "retrying", "reauth_required", "blocked", "paused"]


class HeartbeatSender:
    """One worker, one in-flight request, and at most 100 queued observations."""

    def __init__(
        self, hass: HomeAssistant, entry: AgentConfigEntry, client: AgentClient
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.client = client
        self.status = "paused"
        self.last_heartbeat: datetime | None = None
        self.last_contact: datetime | None = None
        self.last_error: str | None = None
        self.next_attempt: datetime | None = None
        self.failures = 0
        self.dropped_events = 0
        self._queue: deque[dict[str, Any]] = deque(maxlen=MAX_BUFFER_EVENTS)
        self._last_observation: datetime | None = None
        self._listeners: set[Callable[[], None]] = set()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._delay = float(entry.data[CONF_HEARTBEAT_INTERVAL])

    @property
    def pending_events(self) -> int:
        return len(self._queue)

    @property
    def issue_id(self) -> str:
        return f"upload_blocked_{self.entry.entry_id}"

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @callback
    def _async_notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    async def async_setup(self) -> None:
        """Try the first heartbeat before completing setup; retain offline state."""
        if not self.entry.options.get(CONF_ENABLED, True):
            return
        await self.async_send_once(initial=True)

    @callback
    def async_start(self) -> None:
        """Start only after platforms loaded, with entry-scoped cancellation."""
        if self.status not in {"online", "retrying"}:
            return
        self._task = self.entry.async_create_background_task(
            self.hass, self._async_run(), "HA Monitor heartbeat", eager_start=False
        )

    async def _async_run(self) -> None:
        while not self._stopped and self.status in {"online", "retrying"}:
            await asyncio.sleep(self._delay)
            if not self._stopped and self.status in {"online", "retrying"}:
                await self.async_send_once()

    async def async_stop(self) -> None:
        """Cancel and await any in-flight request before returning from unload."""
        self._stopped = True
        self.next_attempt = None
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._listeners.clear()
        self._queue.clear()

    def _envelope(self, events: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "instance_id": self.entry.data[CONF_INSTANCE_ID],
            "integration_version": VERSION,
            "sent_at": now.isoformat(),
            "events": events,
        }

    def _prepare_batch(self, now: datetime) -> tuple[bytes, list[dict[str, Any]]]:
        """Expire old data; also discard observations ahead after a clock rollback."""
        valid = [
            event
            for event in self._queue
            if now - timedelta(hours=23)
            <= datetime.fromisoformat(event["occurred_at"])
            <= now
        ]
        self.dropped_events += len(self._queue) - len(valid)
        self._queue = deque(valid, maxlen=MAX_BUFFER_EVENTS)
        interval = self.entry.data[CONF_HEARTBEAT_INTERVAL]
        if (
            self._last_observation is None
            or self._last_observation > now
            or (now - self._last_observation).total_seconds() >= interval
        ):
            if not all(
                re.fullmatch(r"[0-9][A-Za-z0-9.+_-]{0,31}", version)
                for version in (HA_VERSION, VERSION)
            ):
                raise ProtocolError("invalid_version")
            self.dropped_events += int(len(self._queue) == MAX_BUFFER_EVENTS)
            self._queue.append(
                {
                    "event_id": str(uuid4()),
                    "type": "heartbeat",
                    "occurred_at": now.isoformat(),
                    "data": {"ha_version": HA_VERSION},
                }
            )
            self._last_observation = now
        events: list[dict[str, Any]] = []
        body = b""
        # A larger advertised limit is only an upper bound. Keep every request
        # compatible with the original receiver: exactly one heartbeat event.
        for event in list(self._queue)[
            : min(1, self.entry.data[CONF_MAX_BATCH_EVENTS])
        ]:
            candidate = encode_payload(self._envelope([*events, event], now))
            if len(candidate) > self.entry.data[CONF_MAX_BODY_BYTES]:
                break
            events.append(event)
            body = candidate
        if not events:
            raise ProtocolError("payload_too_large")
        return body, events

    async def async_send_once(self, *, initial: bool = False) -> None:
        """Transmit or record a safe failure; never acknowledge missing events."""
        now = dt_util.utcnow()
        try:
            body, events = self._prepare_batch(now)
            received_at = await self.client.async_ingest(
                body, self.entry.data[CONF_AGENT_TOKEN], len(events)
            )
        except AuthenticationError:
            self.status = "reauth_required"
            self.last_error = "invalid_auth"
            self.next_attempt = None
            self._async_notify()
            if initial:
                raise ConfigEntryAuthFailed(
                    translation_domain=DOMAIN, translation_key="invalid_auth"
                ) from None
            self.entry.async_start_reauth(self.hass)
            return
        except ProtocolError as err:
            self.status = "blocked"
            self.last_error = str(err)
            self.next_attempt = None
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self.issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="upload_blocked",
                translation_placeholders={"reason": str(err)},
            )
            _LOGGER.error("Heartbeat uploads stopped (%s); see Repairs", err)
            self._async_notify()
            return
        except TransportError as err:
            self.failures += 1
            # Equal jitter, bounded exponent and delay; Retry-After is a floor.
            backoff = min(BACKOFF_CAP, BACKOFF_BASE * 2 ** min(self.failures - 1, 10))
            self._delay = max(
                random.uniform(backoff / 2, backoff), err.retry_after or 0
            )
            if self.status != "retrying":
                _LOGGER.warning("Heartbeat receiver unavailable (%s); retrying", err)
            self.status = "retrying"
            self.last_error = str(err)
        else:
            ir.async_delete_issue(self.hass, DOMAIN, self.issue_id)
            for _ in events:
                self._queue.popleft()
            self.last_contact = received_at
            self.last_heartbeat = max(
                datetime.fromisoformat(event["occurred_at"]) for event in events
            )
            if self.status == "retrying":
                _LOGGER.info("Heartbeat receiver available again")
            self.status = "online"
            self.last_error = None
            self.failures = 0
            # At most 20 requests/min while draining a split backlog.
            self._delay = 3 if self._queue else self.entry.data[CONF_HEARTBEAT_INTERVAL]
        self.next_attempt = dt_util.utcnow() + timedelta(seconds=self._delay)
        self._async_notify()
