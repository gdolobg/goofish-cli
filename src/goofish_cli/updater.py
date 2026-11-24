"""CLI 专用的 Maxuria Release 升级入口。"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests
import typer

from goofish_cli import __version__
from goofish_cli.sandbox import cli as sandbox

REPOSITORY = "gdolobg/goofish-cli"
TAG_PATTERN = r"v(\d+\.\d+\.\d+)-maxuria\.(\d{8})(\.\d+)?"


def _download(url: str) -> bytes:
    prefix = f"https://github.com/{REPOSITORY}/releases/download/"
    if not url.startswith(prefix):
        raise ValueError("Release 下载地址不属于 Goofish Maxuria 仓库")
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    return response.content


def _run(*args: str) -> None:
    subprocess.run([sys.executable, *args], check=True)


def update() -> None:
    """升级到最新 Maxuria 分发版，重建已有沙盒并保留登录态卷。"""
    sandbox._host_only()
    if os.environ.get("GOOFISH_DESKTOP_SANDBOX") == "1":
        sandbox._fail("请在宿主机执行 goofish update")
    try:
        response = requests.get(
            f"https://api.github.com/repos/{REPOSITORY}/releases/latest", timeout=30,
        )
        response.raise_for_status()
        release = response.json()
        match = re.fullmatch(TAG_PATTERN, release["tag_name"])
        if not match or release.get("draft") or release.get("prerelease"):
            raise ValueError("最新 Release 不是受支持的 Maxuria 正式分发版")
        version = f"{match[1]}+maxuria.{match[2]}{match[3] or ''}"
        # Maxuria 版本按基础版本、日期、当日修订号排列，禁止自动降级。
        def version_key(value: str) -> tuple[int, ...]:
            return tuple(map(int, re.findall(r"\d+", value)))

        if version_key(version) <= version_key(__version__):
            typer.echo(f"当前版本 {__version__} 已是最新或更新版本。")
            return
        name = f"goofish_cli-{version}-py3-none-any.whl"
        assets = {asset["name"]: asset["browser_download_url"] for asset in release["assets"]}
        checksums = _download(assets["SHA256SUMS.txt"]).decode("utf-8")
        entries = [line.split() for line in checksums.splitlines()]
        hashes = [parts[0] for parts in entries if len(parts) == 2 and parts[1] == name]
        if len(hashes) != 1 or not re.fullmatch(r"[0-9a-fA-F]{64}", hashes[0]):
            raise ValueError("校验清单缺少唯一有效的 wheel SHA256")
        wheel = _download(assets[name])
        if hashlib.sha256(wheel).hexdigest() != hashes[0].lower():
            raise ValueError("wheel SHA256 校验失败，未安装")

        info = None
        if shutil.which("docker"):
            sandbox._ensure_docker()
            info = sandbox._container()
        port = None
        if info:
            sandbox._viewer(info)  # 验证容器归属和端口绑定后才更新。
            port = info["HostConfig"]["PortBindings"]["6080/tcp"][0]["HostPort"]

        typer.echo(f"升级 {__version__} → {version}，Python：{sys.executable}")
        with tempfile.TemporaryDirectory(prefix="goofish-update-") as directory:
            path = Path(directory) / name
            path.write_bytes(wheel)
            _run("-m", "pip", "install", "--upgrade", str(path))
        if port is not None:
            typer.echo("正在重建沙盒，保留 goofish-sandbox-state 登录态卷。")
            try:
                _run("-m", "goofish_cli.cli", "sandbox", "stop")
                _run("-m", "goofish_cli.cli", "sandbox", "start", "--rebuild", "--port", port)
            except subprocess.CalledProcessError:
                sandbox._fail(
                    "CLI 已升级，但沙盒重建失败；登录态卷保留。请修复后执行 "
                    f"goofish sandbox stop，再执行 goofish sandbox start --rebuild --port {port}。"
                )
        typer.echo(f"已升级到 {version}。请重新连接 Codex 中的 Goofish MCP。")
    except (requests.RequestException, ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        sandbox._fail(f"升级失败：{exc}")
