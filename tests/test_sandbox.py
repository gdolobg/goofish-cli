"""沙箱 CLI 的参数、失败语义与资源所有权边界。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from goofish_cli.cli import app
from goofish_cli.core.browser import goofish_page
from goofish_cli.core.errors import AuthRequiredError, GoofishError
from goofish_cli.sandbox import cli

runner = CliRunner()


def test_build_context_normalizes_windows_script_line_endings(tmp_path, monkeypatch):
    package = tmp_path / 'source'
    package.mkdir()
    (package / '__init__.py').write_text('')
    (package / '_bundled_skills').mkdir()
    assets = tmp_path / 'assets'
    assets.mkdir()
    for name in ('Dockerfile', 'entrypoint.sh'):
        (assets / name).write_bytes(b'#!/bin/bash\r\nexit 0\r\n')
    monkeypatch.setattr(cli.goofish_cli, '__file__', str(package / '__init__.py'))
    monkeypatch.setattr(cli, 'ASSETS', assets)
    monkeypatch.setattr(cli, 'requires', lambda name: [])
    monkeypatch.setattr(cli, 'distribution', lambda name: SimpleNamespace(files=[]))
    destination = tmp_path / 'context'
    destination.mkdir()
    cli._build_context(destination)
    assert (destination / 'entrypoint.sh').read_bytes() == b'#!/bin/bash\nexit 0\n'


def _running():
    return {
        "Config": {"Labels": {cli.OWNER: "true"}},
        "State": {"Running": True},
        "HostConfig": {
            "PortBindings": {"6080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "16080"}]}
        },
    }


def test_run_preserves_arguments_and_child_exit_code(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_inspect", lambda *a: _running())

    def docker(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=23)

    monkeypatch.setattr(cli, "_docker", docker)
    query = "机械键盘; $(do-not-execute)"
    result = runner.invoke(app, ["sandbox", "run", "search", "items", query, "--limit", "3"])
    assert result.exit_code == 23
    assert calls == [
        ("exec", "-i", cli.CONTAINER, "goofish", "search", "items", query, "--limit", "3")
    ]


def test_run_forwards_business_help(monkeypatch):
    monkeypatch.setattr(cli, "_inspect", lambda *a: _running())
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli, "_docker", docker)
    result = runner.invoke(app, ["sandbox", "run", "search", "items", "--help"])
    assert result.exit_code == 0
    assert calls == [("exec", "-i", cli.CONTAINER, "goofish", "search", "items", "--help")]


def test_stopped_sandbox_never_runs_a_local_command(monkeypatch):
    monkeypatch.setattr(cli, "_inspect", lambda *a: None)
    monkeypatch.setattr(cli, "_docker", lambda *a, **kw: pytest.fail("不能执行业务命令"))
    result = runner.invoke(app, ["sandbox", "run", "auth", "status"])
    assert result.exit_code == 1
    assert "沙箱未运行" in result.output


def test_unowned_container_is_not_stopped(monkeypatch):
    info = _running()
    info["Config"]["Labels"] = {}
    monkeypatch.setattr(cli, "_inspect", lambda *a: info)
    monkeypatch.setattr(cli, "_docker", lambda *a, **kw: pytest.fail("不能修改其他容器"))
    result = runner.invoke(app, ["sandbox", "stop"])
    assert result.exit_code == 1
    assert "不属于 goofish" in result.output


def test_unowned_state_volume_is_not_reused(monkeypatch):
    monkeypatch.setattr(
        cli, "_inspect", lambda kind, name: {"Labels": {}} if kind == "volume" else None
    )
    monkeypatch.setattr(cli, "_docker", lambda *a, **kw: pytest.fail("不能修改其他卷"))
    result = runner.invoke(app, ["sandbox", "start"])
    assert result.exit_code == 1
    assert "不属于 goofish" in result.output


def test_public_viewer_binding_is_rejected_before_start(monkeypatch):
    info = _running()
    info["State"]["Running"] = False
    info["HostConfig"]["PortBindings"]["6080/tcp"][0]["HostIp"] = "0.0.0.0"
    monkeypatch.setattr(cli, "_inspect", lambda *a: info)
    monkeypatch.setattr(cli, "_docker", lambda *a, **kw: pytest.fail("不能启动公开观察端口"))
    result = runner.invoke(app, ["sandbox", "start"])
    assert result.exit_code == 1
    assert "127.0.0.1" in result.output


def test_mcp_transport_has_no_stdout_banner(monkeypatch):
    monkeypatch.setattr(cli, "_inspect", lambda *a: _running())
    monkeypatch.setattr(cli, "_ensure_docker", lambda: None)
    monkeypatch.setattr(cli, "_ready", lambda *a: None)
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli, "_docker", docker)
    result = runner.invoke(app, ["sandbox", "mcp"])
    assert result.exit_code == 0
    assert result.stdout == ""
    assert calls == [("exec", "-i", cli.CONTAINER, "goofish-cli")]


def test_mcp_bootstraps_before_connecting(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_ensure_docker", lambda: calls.append("docker"))

    def start(**kwargs):
        calls.append("sandbox")
        typer.echo("viewer link")

    monkeypatch.setattr(cli, "start", start)
    monkeypatch.setattr(cli, "_docker", lambda *a, **kw: calls.append("mcp") or SimpleNamespace(returncode=0))
    result = runner.invoke(app, ["sandbox", "mcp"])
    assert result.exit_code == 0
    assert calls == ["docker", "sandbox", "mcp"]
    assert result.stdout == ""
    assert "viewer link" in result.stderr


def test_docker_already_running_skips_launch(monkeypatch):
    monkeypatch.setattr(cli, "_docker_ready", lambda: True)
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **kw: pytest.fail("不应重复启动"))
    cli._ensure_docker()


def test_windows_docker_launch_waits_until_ready(monkeypatch):
    states = iter([False, False, True])
    monkeypatch.setattr(cli, "_docker_ready", lambda: next(states))
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_windows_desktop_environment", lambda: {"ProgramData": "C:/ProgramData"})
    monkeypatch.setattr(cli.Path, "is_file", lambda self: True)
    monkeypatch.setattr(cli.time, "sleep", lambda seconds: None)
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda command, **kw: calls.append(kw["env"]["GOOFISH_DOCKER_DESKTOP"]) or SimpleNamespace(returncode=0))
    cli._ensure_docker()
    assert len(calls) == 1
    assert calls[0].endswith("Docker Desktop.exe")


def test_docker_start_timeout_stops_connection(monkeypatch):
    monkeypatch.setattr(cli, "_docker_ready", lambda: False)
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *a, **kw: None)
    ticks = iter([0, 121])
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticks))
    with pytest.raises(typer.Exit):
        cli._ensure_docker()


def test_windows_docker_uses_independent_process(monkeypatch):
    states = iter([False, True])
    monkeypatch.setattr(cli, "_docker_ready", lambda: next(states))
    monkeypatch.setattr(cli.sys, "platform", "win32")
    monkeypatch.setattr(cli, "_windows_desktop_environment", lambda: {"ProgramData": "C:/ProgramData"})
    monkeypatch.setattr(cli.Path, "is_file", lambda self: True)
    calls = []
    monkeypatch.setattr(cli.subprocess, "run", lambda args, **kw: calls.append((args, kw)) or SimpleNamespace(returncode=0))
    cli._ensure_docker()
    args, options = calls[0]
    assert "Win32_Process" in args[-1]
    assert "EnvironmentVariables" in args[-1]
    assert options["env"]["GOOFISH_DOCKER_DESKTOP"].endswith("Docker Desktop.exe")
    assert options["creationflags"] == 0x08000000


def test_interactive_mcp_does_not_start_docker(monkeypatch):
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli, "_docker", lambda *a, **kw: pytest.fail("TTY 误用不应启动 MCP"))
    with pytest.raises(typer.Exit) as error:
        cli.mcp()
    assert error.value.exit_code == 2


def test_missing_docker_reports_error_without_browser_fallback(monkeypatch):
    def missing(*a, **kw):
        raise FileNotFoundError

    monkeypatch.setattr(cli.subprocess, "run", missing)
    result = runner.invoke(app, ["sandbox", "start"])
    assert result.exit_code == 1
    assert "找不到 Docker" in result.output


@pytest.mark.parametrize("valid,exit_code", [(True, 0), (False, 77)])
def test_login_checks_remote_validity_without_printing_account_data(monkeypatch, valid, exit_code):
    monkeypatch.setattr(cli, "_inspect", lambda *a: _running())
    monkeypatch.setattr(cli, "_ready", lambda *a: None)
    responses = iter(
        [
            SimpleNamespace(returncode=0, stdout='{"unb":"private-account"}'),
            SimpleNamespace(
                returncode=0, stdout='{"valid":' + str(valid).lower() + ',"nick":"private-nick"}'
            ),
        ]
    )
    monkeypatch.setattr(cli, "_docker", lambda *a, **kw: next(responses))
    result = runner.invoke(app, ["sandbox", "login"])
    assert result.exit_code == exit_code
    assert "private-account" not in result.output
    assert "private-nick" not in result.output


async def test_sandbox_runtime_rejects_missing_virtual_display(monkeypatch):
    monkeypatch.setenv("GOOFISH_DESKTOP_SANDBOX", "1")
    monkeypatch.setenv("DISPLAY", ":99")
    with pytest.raises(GoofishError, match="不会回退"):
        async with goofish_page(cookies={}):
            pytest.fail("沙箱环境失效时不能启动浏览器")


async def test_missing_login_does_not_leave_temporary_profiles(monkeypatch, tmp_path):
    from goofish_cli.core import browser

    monkeypatch.delenv("GOOFISH_DESKTOP_SANDBOX", raising=False)
    monkeypatch.setattr(browser, "PROFILES_PARENT", tmp_path)

    def missing():
        raise AuthRequiredError("测试登录态缺失")

    monkeypatch.setattr(browser, "_load_cookies_from_session", missing)
    with pytest.raises(AuthRequiredError):
        async with goofish_page():
            pytest.fail("没有登录态时不能启动浏览器")
    assert list(tmp_path.iterdir()) == []
