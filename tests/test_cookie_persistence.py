"""中断或旧浏览器快照不能破坏新登录态。"""

import hashlib
import os
import stat

import pytest
from filelock import FileLock

from goofish_cli.core.crypto import decrypt_cookies
from goofish_cli.core.session import save_cookies_if_unchanged, write_cookies_json


def test_stale_snapshot_cannot_overwrite_new_login(tmp_path):
    path = tmp_path / "cookies.json"
    write_cookies_json(path, {"unb": "first"})
    previous = hashlib.sha256(path.read_bytes()).hexdigest()
    write_cookies_json(path, {"unb": "second"})
    assert not save_cookies_if_unchanged(path, {"unb": "first"}, previous)
    assert decrypt_cookies(path.read_bytes())[0]["value"] == "second"
    current = hashlib.sha256(path.read_bytes()).hexdigest()
    assert save_cookies_if_unchanged(path, {"unb": "second", "token": "new"}, current)
    # Windows 的 chmod/stat 不支持 POSIX 用户组权限位。
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_failed_replace_preserves_previous_encrypted_file(tmp_path, monkeypatch):
    from goofish_cli.core import local_state

    path = tmp_path / "cookies.json"
    write_cookies_json(path, {"unb": "first"})
    previous = path.read_bytes()

    def fail(*args):
        raise OSError("test disk failure")

    monkeypatch.setattr(local_state.os, "replace", fail)
    with pytest.raises(OSError):
        write_cookies_json(path, {"unb": "second"})
    assert path.read_bytes() == previous
    # Windows 解锁后可能删除锁文件；两种情况下均不能残留临时文件。
    assert {p.name for p in tmp_path.iterdir()} - {"cookies.json.lock"} == {"cookies.json"}
    with FileLock(str(path) + ".lock", timeout=0):
        assert path.read_bytes() == previous


def test_restart_only_restores_missing_allowed_cookies_with_native_attributes():
    from goofish_cli.core.sandbox_browser import _missing_cookies

    token = {"name": "token", "value": "old", "domain": ".goofish.com", "path": "/"}
    session = {
        "name": "session",
        "value": "saved",
        "domain": ".goofish.com",
        "path": "/",
        "expires": -1,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }
    foreign = {**session, "domain": ".unrelated.example"}
    current = [{**token, "value": "new"}]
    assert _missing_cookies([token, session, foreign], current) == [session]
