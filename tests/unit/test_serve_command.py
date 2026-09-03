"""Tests for the `serve` CLI command's own logic: config resolution and uvicorn invocation.

``uvicorn.run`` itself is never called for real here — it blocks forever — so this asserts what
``promptcadence serve`` hands it, not that a socket actually opens (the e2e suite proves that by
building the app object directly through :func:`promptcadence.bootstrap.bootstrap`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from promptcadence.cli.main import app

runner = CliRunner()


def test_serve_invokes_uvicorn_with_resolved_host_and_port(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    def _fake_run(target: str, **kwargs: Any) -> None:
        calls.append({"target": target, **kwargs})

    monkeypatch.setattr("uvicorn.run", _fake_run)
    result = runner.invoke(app, ["serve", "--host", "127.0.0.1", "--port", "18999"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0]["target"] == "promptcadence.bootstrap:create_app_from_environment"
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["port"] == 18999
    assert calls[0]["factory"] is True


def test_serve_accepts_an_explicit_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text("[server]\nport = 19000\n")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda target, **kw: calls.append(kw))
    result = runner.invoke(app, ["serve", "--config", str(config_file)])
    assert result.exit_code == 0, result.output
    assert calls[0]["port"] == 19000


def test_serve_exits_three_on_invalid_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "bad.toml"
    config_file.write_text('[server]\nhost = "0.0.0.0"\n')  # unacknowledged LAN exposure
    monkeypatch.setattr("uvicorn.run", lambda target, **kw: None)
    result = runner.invoke(app, ["serve", "--config", str(config_file)])
    assert result.exit_code == 3
    assert "INSECURE_BINDING" in result.stderr


def test_root_with_no_subcommand_defaults_to_serve(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr("uvicorn.run", lambda target, **kw: calls.append(kw))
    result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
