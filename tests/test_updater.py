import hashlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests
from typer.testing import CliRunner

from goofish_cli import updater as up
from goofish_cli.cli import app
from goofish_cli.core.registry import discover, registry

VERSION = "0.6.3+maxuria.20990101"
NAME = f"goofish_cli-{VERSION}-py3-none-any.whl"
PREFIX = f"https://github.com/{up.REPOSITORY}/releases/download/v0.6.3-maxuria.20990101/"


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.delenv("GOOFISH_DESKTOP_SANDBOX", raising=False)
    monkeypatch.setattr(up, "__version__", "0.6.3+maxuria.20261124")
    wheel = b"synthetic-wheel"
    release = {
        "tag_name": "v0.6.3-maxuria.20990101",
        "assets": [{"name": name, "browser_download_url": PREFIX + name}
                   for name in (NAME, "SHA256SUMS.txt")],
    }
    def get(url, **kwargs):
        if url.endswith("/releases/latest"):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: release)
        data = wheel if url.endswith(".whl") else (
            hashlib.sha256(wheel).hexdigest() + "  " + NAME + "\n"
        ).encode()
        return SimpleNamespace(raise_for_status=lambda: None, content=data)

    monkeypatch.setattr(up.requests, "get", get)
    monkeypatch.setattr(up.shutil, "which", lambda _: None)
    calls = []
    def run(*args):
        if args[:3] == ("-m", "pip", "install"):
            from pathlib import Path
            assert Path(args[-1]).read_bytes() == wheel
        calls.append(args)
    monkeypatch.setattr(up, "_run", run)
    return release, calls


def test_update_installs_current_environment_and_is_cli_only(setup):
    _, calls = setup
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][:4] == ("-m", "pip", "install", "--upgrade")
    assert "重新连接" in result.output
    discover()
    assert all(command.name != "update" for command in registry().values())


def test_update_preserves_volume_and_port(setup, monkeypatch):
    _, calls = setup
    monkeypatch.setattr(up.shutil, "which", lambda _: "docker")
    monkeypatch.setattr(up.sandbox, "_ensure_docker", lambda: None)
    monkeypatch.setattr(up.sandbox, "_container", lambda: {
        "HostConfig": {"PortBindings": {"6080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "17080"}]}}
    })
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0, result.output
    assert calls[1:] == [
        ("-m", "goofish_cli.cli", "sandbox", "stop"),
        ("-m", "goofish_cli.cli", "sandbox", "start", "--rebuild", "--port", "17080"),
    ]
    assert "登录态卷" in result.output


@pytest.mark.parametrize("failure", ["hash", "missing", "tag", "url"])
def test_update_rejects_invalid_release_before_install(setup, monkeypatch, failure):
    release, calls = setup
    if failure == "hash":
        original = up._download
        monkeypatch.setattr(up, "_download", lambda url: b"tampered" if url.endswith(".whl") else original(url))
    elif failure == "missing":
        release["assets"] = []
    elif failure == "tag":
        release["tag_name"] = "v0.6.4"
    else:
        release["assets"][1]["browser_download_url"] = "https://example.com/SHA256SUMS.txt"
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 1
    assert calls == []


def test_up_to_date_does_not_install_or_start_docker(setup, monkeypatch):
    _, calls = setup
    monkeypatch.setattr(up, "__version__", VERSION)
    monkeypatch.setattr(up.shutil, "which", Mock(side_effect=AssertionError("Docker checked")))
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0
    assert calls == []


def test_network_error_does_not_install(setup, monkeypatch):
    _, calls = setup
    monkeypatch.setattr(up.requests, "get", Mock(side_effect=requests.ConnectionError("offline")))
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 1
    assert calls == []


def test_pip_failure_does_not_stop_sandbox(setup, monkeypatch):
    import subprocess
    run = Mock(side_effect=subprocess.CalledProcessError(1, "pip"))
    monkeypatch.setattr(up, "_run", run)
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 1
    assert run.call_count == 1


def test_rebuild_failure_has_recovery_instructions(setup, monkeypatch):
    import subprocess
    monkeypatch.setattr(up.shutil, "which", lambda _: "docker")
    monkeypatch.setattr(up.sandbox, "_ensure_docker", lambda: None)
    monkeypatch.setattr(up.sandbox, "_container", lambda: {
        "HostConfig": {"PortBindings": {"6080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "17080"}]}}
    })
    def run(*args):
        if "start" in args:
            raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(up, "_run", run)
    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 1
    assert "CLI 已升级" in result.output
    assert "--rebuild --port 17080" in result.output
