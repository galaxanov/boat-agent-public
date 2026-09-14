"""Signal K WebSocket client.

Strictly read-only. The only frame this ever sends is the subscription in
paths.subscription_message(). Nothing here may send a PUT or any other command:
writes to the N2K bus need an explicit confirm flag and a deliberate code path,
not something that can happen by accident from the monitoring loop.

Reconnects forever with capped exponential backoff. Signal K restarts, the
NGX-1's USB re-enumerates, Starlink drops - none of it should need a human.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus, WebSocketException

from .config import Config
from .paths import subscription_message
from .state import BoatState

log = logging.getLogger(__name__)

Callback = Callable[[], Awaitable[None]] | None

# websockets pings on this interval and closes the connection if a pong does not
# come back. That is what catches a WiFi drop where the TCP socket just hangs.
PING_INTERVAL = 20.0
PING_TIMEOUT = 20.0
OPEN_TIMEOUT = 15.0

# A connection that survived this long counts as healthy, so the next failure
# starts backing off from scratch. Without it, a link that drops once every few
# hours would creep up to the maximum delay and stay there.
STABLE_SECONDS = 60.0


class SignalKClient:
    def __init__(
        self,
        config: Config,
        state: BoatState,
        on_connect: Callback = None,
        on_disconnect: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.config = config
        self.state = state
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self.connected = False
        self.connect_attempts = 0
        self._connected_at: float | None = None

    async def run(self, stop: asyncio.Event) -> None:
        """Connect, subscribe, consume - and do it again whenever it breaks."""
        backoff = self.config.reconnect_min

        while not stop.is_set():
            self.connect_attempts += 1
            reason = "closed"
            self._connected_at = None
            try:
                await self._session(stop)
            except asyncio.CancelledError:
                raise
            except InvalidStatus as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (401, 403):
                    reason = f"rejected: HTTP {status}"
                    log.error(
                        "Signal K refused the connection (HTTP %s). Security is on and this "
                        "agent has no token - set SIGNALK_TOKEN in .env, or allow readonly "
                        "access in the admin UI.",
                        status,
                    )
                else:
                    reason = f"HTTP {status}"
                    log.warning("Signal K handshake failed: %s", exc)
            except (WebSocketException, OSError) as exc:
                reason = f"{type(exc).__name__}: {exc}"
                # Expected on a boat, so this is not an error-level event.
                log.warning("Signal K connection lost (%s)", reason)
            except json.JSONDecodeError as exc:
                reason = f"bad frame: {exc}"
                log.warning("Signal K sent something that is not JSON: %s", exc)
            finally:
                if self.connected:
                    self.connected = False
                    if self._on_disconnect is not None:
                        await self._on_disconnect(reason)

            if stop.is_set():
                break

            if self._connected_at is not None:
                uptime = time.monotonic() - self._connected_at
                if uptime >= STABLE_SECONDS:
                    log.info("connection had been up %.0fs, resetting backoff", uptime)
                    backoff = self.config.reconnect_min

            # Jitter so a Pi and a laptop reconnecting together do not sync up.
            delay = min(backoff, self.config.reconnect_max) * random.uniform(0.8, 1.2)
            log.info("reconnecting to %s in %.1fs", self.config.signalk_host, delay)
            # An interruptible sleep: a stop during the backoff wait ends it.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)
            backoff = min(backoff * 2, self.config.reconnect_max)

    async def _session(self, stop: asyncio.Event) -> None:
        url = self.config.ws_url
        log.info("connecting to %s", url)

        async with connect(
            url,
            additional_headers=self.config.headers,
            ping_interval=PING_INTERVAL,
            ping_timeout=PING_TIMEOUT,
            open_timeout=OPEN_TIMEOUT,
            close_timeout=5,
            max_queue=256,
        ) as ws:
            self.connected = True
            self._connected_at = time.monotonic()
            log.info("connected to Signal K at %s", self.config.signalk_host)
            if self._on_connect is not None:
                await self._on_connect()

            if self.config.discover:
                log.warning("discover mode: subscribed to every self path, not the curated list")
            else:
                message = subscription_message()
                await ws.send(json.dumps(message))
                log.info("subscribed to %d paths", len(message["subscribe"]))

            await self._consume(ws, stop)

    async def _consume(self, ws: Any, stop: asyncio.Event) -> None:
        """Read frames until the socket closes or we are asked to stop.

        `async for raw in ws` would only notice the stop event when the next
        frame arrives. At anchor overnight the bus can be quiet for minutes,
        which would leave the agent ignoring SIGTERM until systemd lost
        patience and killed it. So race the read against the stop event.
        """
        stop_task = asyncio.create_task(stop.wait(), name="stop")
        try:
            while True:
                recv_task = asyncio.create_task(ws.recv(), name="recv")
                done, _ = await asyncio.wait(
                    {recv_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if stop_task in done:
                    recv_task.cancel()
                    await asyncio.gather(recv_task, return_exceptions=True)
                    log.info("shutting down, closing the Signal K connection")
                    await ws.close()
                    return
                self._handle(recv_task.result())
        finally:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)

    def _handle(self, raw: str | bytes) -> None:
        try:
            message: Any = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            log.warning("dropping unparseable frame: %s", exc)
            return

        if not isinstance(message, dict):
            return

        if "updates" in message:
            self.state.apply_delta(message)
        elif "self" in message or "version" in message:
            self.state.apply_hello(message)
        # Anything else (subscription acks, request responses) is not our
        # business while the agent is read-only.
