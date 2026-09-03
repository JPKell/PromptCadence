"""Shared fixtures.

Two properties every test in this suite depends on:

* **No network, ever.** The default suite must pass with no LoadCoach reachable and no network
  (spec §18, development plan Phase 1), so the ``no_network`` autouse fixture makes an accidental
  non-loopback socket a loud failure rather than a slow one. A test that genuinely needs one is
  marked ``live``.
* **No ambient state.** Configuration reads ``PROMPTCADENCE_*`` and the XDG variables from the real
  environment, so every test gets its own data directory and a cleared prefix. Without this a
  developer's own ``~/.config/promptcadence/config.toml`` would change the result of a test run.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_REAL_SOCKET_CONNECT = socket.socket.connect


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse any outbound socket connection that is not to loopback.

    Tests marked ``live`` are exempt: those are the ones that need a real provider.
    """
    if request.node.get_closest_marker("live"):
        return

    def _guard(self: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host in {"127.0.0.1", "::1", "localhost"}:
            _REAL_SOCKET_CONNECT(self, address)
            return
        message = f"network access refused in a default test: {address!r}"
        raise RuntimeError(message)

    monkeypatch.setattr(socket.socket, "connect", _guard)


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every XDG path at a temporary directory and clear the ``PROMPTCADENCE_`` prefix."""
    for key in list(os.environ):
        if key.startswith("PROMPTCADENCE_"):
            monkeypatch.delenv(key, raising=False)
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("PROMPTCADENCE_DATA_DIR", str(data))
    monkeypatch.chdir(tmp_path)
    data.mkdir(parents=True, exist_ok=True)
    yield data
