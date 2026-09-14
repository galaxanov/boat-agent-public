from __future__ import annotations

import asyncio
import json

import pytest

from agent.config import Config
from agent.state import BoatState

websockets = pytest.importorskip("websockets", reason="runtime dependency, installed on the Pi")

from agent import signalk  # noqa: E402  (must come after the importorskip)


class FakeSocket:
    """Hands out recorded frames, then blocks like a quiet bus."""

    def __init__(self, frames: list[str]) -> None:
        self.frames = list(frames)
        self.sent: list[str] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True

    async def recv(self) -> str:
        if self.frames:
            return self.frames.pop(0)
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class FakeConnect:
    def __init__(self, socket: FakeSocket) -> None:
        self.socket = socket

    def __call__(self, _url: str, **_kwargs: object) -> FakeConnect:
        return self

    async def __aenter__(self) -> FakeSocket:
        return self.socket

    async def __aexit__(self, *_exc: object) -> bool:
        return False


def run_session(monkeypatch, frames: list[str], stop_after: float = 0.3) -> tuple:
    """Run one client session against a fake socket, then stop it."""
    state = BoatState()
    socket = FakeSocket(frames)
    monkeypatch.setattr(signalk, "connect", FakeConnect(socket))
    client = signalk.SignalKClient(Config.from_env({}), state)

    async def drive() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await asyncio.sleep(stop_after)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())
    return state, socket


def test_the_only_frame_sent_is_the_subscription(monkeypatch, deltas: list[dict]) -> None:
    """The agent is read-only: it must never put anything on the bus."""
    _state, socket = run_session(monkeypatch, [json.dumps(d) for d in deltas])

    assert len(socket.sent) == 1
    message = json.loads(socket.sent[0])
    assert set(message) == {"context", "subscribe"}
    assert message["context"] == "vessels.self"


def test_frames_reach_the_state_model(monkeypatch, deltas: list[dict]) -> None:
    state, _socket = run_session(monkeypatch, [json.dumps(d) for d in deltas])

    assert state.self_context is not None
    assert state.value("environment.depth.belowTransducer") == 10.9
    assert state.value("electrical.solar.mppt.chargingMode") == "bulk"


def test_stop_closes_the_socket_on_a_quiet_bus(monkeypatch) -> None:
    """No frames at all: shutdown must not wait for one to arrive."""
    _state, socket = run_session(monkeypatch, [], stop_after=0.2)
    assert socket.closed is True


def test_discover_mode_sends_nothing(monkeypatch) -> None:
    state = BoatState()
    socket = FakeSocket([])
    monkeypatch.setattr(signalk, "connect", FakeConnect(socket))
    client = signalk.SignalKClient(Config.from_env({"AGENT_DISCOVER": "1"}), state)

    async def drive() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(client.run(stop))
        await asyncio.sleep(0.2)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())
    assert socket.sent == []


def test_junk_frames_do_not_kill_the_client(monkeypatch, hello: dict) -> None:
    frames = [
        "not json at all",
        "[1, 2, 3]",
        json.dumps(hello),
        json.dumps({"unrecognised": "frame"}),
        json.dumps(
            {"context": "vessels.self", "updates": [{"values": [{"path": "x", "value": 1}]}]}
        ),
    ]
    state, _socket = run_session(monkeypatch, frames)
    assert state.value("x") == 1
