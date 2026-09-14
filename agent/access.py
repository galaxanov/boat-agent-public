"""Getting a read token out of Signal K, without hand-editing anything.

The installer starts Signal K with security enabled, which is the right
default on a boat that carries a Starlink dish and a WiFi network other people
can see. The consequence is that an agent with no token is not merely refused
the REST API: it can hold a WebSocket open, subscribe, and be told nothing at
all. A monitoring agent that is silently blind is the worst failure in this
codebase, because every alarm stays quiet and the log fills with snapshots of
an empty state model.

So two things live here. `request_token` walks the standard Signal K device
access request: the agent asks, a human approves it once in the admin UI, and
the server hands back a token that does not expire. `check_read_access` is the
cheap probe the agent runs at startup so that being blind is loud rather than
quiet.

Neither ever raises into the agent. No token is not a reason to stop watching
the things that do not need one.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# A stable id per boat, so asking twice does not queue a second request and an
# approval survives re-running the command.
CLIENT_NAMESPACE = uuid.UUID("6f1d5b2e-6a3d-4b0b-9a3e-1f5f5f0c6a11")

POLL_SECONDS = 3.0
POLL_TIMEOUT = 600.0
HTTP_TIMEOUT = 10.0


def client_id(hostname: str | None = None) -> str:
    return str(uuid.uuid5(CLIENT_NAMESPACE, f"boat-agent.{hostname or socket.gethostname()}"))


@dataclass
class Probe:
    """What the server said when asked for a reading."""

    ok: bool
    status: int | None = None
    detail: str = ""

    @property
    def unauthorized(self) -> bool:
        return self.status == 401


def _get(url: str, headers: dict[str, str], timeout: float = HTTP_TIMEOUT) -> Any:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def check_read_access(rest_url: str, headers: dict[str, str]) -> Probe:
    """Can we actually read this server? Never raises."""
    try:
        _get(f"{rest_url}/navigation/position/value", headers)
    except urllib.error.HTTPError as exc:
        # 404 is a fine answer: the server is readable, it just has no fix yet.
        if exc.code == 404:
            return Probe(ok=True, status=404)
        return Probe(ok=False, status=exc.code, detail=str(exc))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return Probe(ok=False, status=None, detail=str(exc))
    return Probe(ok=True, status=200)


def request_token(
    base_url: str,
    description: str = "Boat agent",
    timeout: float = POLL_TIMEOUT,
    poll: float = POLL_SECONDS,
    sleep: Any = time.sleep,
    now: Any = time.monotonic,
) -> tuple[str | None, str]:
    """Ask for read access and wait for a human to approve it.

    Returns the token and a line to show the crew, or None and why not.
    """
    identifier = client_id()
    try:
        body = json.dumps({"clientId": identifier, "description": description}).encode()
        request = urllib.request.Request(
            f"{base_url}/signalk/v1/access/requests",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            reply = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, (
                "this Signal K has security turned off, so no token is needed - "
                "leave SIGNALK_TOKEN empty"
            )
        return None, f"the server refused the access request: {exc}"
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        return None, f"could not reach Signal K at {base_url}: {exc}"

    href = reply.get("href")
    if not isinstance(href, str):
        return None, f"the server gave no request to follow: {reply}"

    log.info(
        "Access requested as %s.\n"
        "    Approve it now at %s -> Security -> Access Requests.\n"
        "    If that page asks you to log in and you have never done so, the admin\n"
        "    login has not been created yet: make it there first, then approve.",
        identifier,
        base_url,
    )

    deadline = now() + timeout
    while now() < deadline:
        sleep(poll)
        try:
            status = _get(f"{base_url}{href}", {})
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            log.debug("still waiting on the approval: %s", exc)
            continue
        except json.JSONDecodeError:
            continue

        if status.get("state") != "COMPLETED":
            continue

        access = status.get("accessRequest") or {}
        permission = str(access.get("permission", "")).upper()
        if permission == "APPROVED":
            token = access.get("token")
            if isinstance(token, str) and token:
                return token, f"approved with {access.get('expiration', 'no')} expiry"
            return None, "approved, but the server sent no token"
        return None, f"the request was {permission.lower() or 'not approved'}"

    return None, "nobody approved it in time - the request is still waiting in the admin UI"
