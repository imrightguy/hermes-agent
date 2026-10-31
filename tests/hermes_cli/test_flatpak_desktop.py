"""Tests for the Linux Flathub Hermes Desktop lifecycle."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import pytest

from hermes_cli import flatpak_desktop
from hermes_cli import main_desktop
from hermes_cli import snap_desktop


def _completed(command, returncode=0, **kwargs):
    return subprocess.CompletedProcess(command, returncode, **kwargs)


def test_ensure_installed_adds_flathub_and_installs_missing_app(monkeypatch):
    commands = []

    monkeypatch.setattr(flatpak_desktop, "_flatpak", lambda: "/usr/bin/flatpak")
    monkeypatch.setattr(
        flatpak_desktop,
        "_run",
        lambda command, **kwargs: commands.append((command, kwargs)) or _completed(command, 1 if command[1:3] == ["info", "--user"] else 0),
    )

    assert flatpak_desktop.ensure_installed() is True
    assert [command for command, _kwargs in commands] == [
        ["/usr/bin/flatpak", "remote-add", "--if-not-exists", "--user", "flathub", flatpak_desktop.FLATHUB_REPO],
        ["/usr/bin/flatpak", "info", "--user", flatpak_desktop.APP_ID],
        ["/usr/bin/flatpak", "install", "--user", "--noninteractive", "flathub", flatpak_desktop.APP_ID],
    ]


def test_launch_with_native_backend_starts_native_backend_and_passes_only_local_connection(monkeypatch, tmp_path):
    commands = []
    ready_path = tmp_path / "ready.json"

    class FakePopen:
        def __init__(self, *args, **kwargs):
            self._poll_result = None
            commands.append(("Popen", args, kwargs))
            # Write the ready file after process creation (simulating backend startup)
            ready_path.write_text(json.dumps({"port": 41234}))

        def poll(self):
            return self._poll_result

        def terminate(self):
            pass

        def wait(self, timeout):
            pass

    # Mock the tempfile to use our controlled ready file
    import tempfile
    import secrets

    def mock_named_temporary_file(*args, **kwargs):
        class FakeFile:
            def __init__(self):
                self.name = str(ready_path)
            def close(self):
                pass
        return FakeFile()

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", mock_named_temporary_file)
    monkeypatch.setattr(secrets, "token_urlsafe", lambda n: "one-time-token")

    monkeypatch.setattr(flatpak_desktop, "ensure_installed", lambda: True)
    monkeypatch.setattr(flatpak_desktop, "_flatpak", lambda: "/usr/bin/flatpak")
    monkeypatch.setattr(flatpak_desktop.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(flatpak_desktop, "_run",
        lambda command, **kwargs: commands.append((command, kwargs)) or _completed(command),
    )

    backend_cmd = ["/native/hermes"]
    result = flatpak_desktop.launch_with_native_backend(backend_cmd, cwd=str(tmp_path))

    assert result == 0
    flatpak_call = commands[-1]
    command, kwargs = flatpak_call
    assert command == [
        "/usr/bin/flatpak", "run",
        "--env=HERMES_DESKTOP_REMOTE_URL=http://127.0.0.1:41234",
        "--env=HERMES_DESKTOP_REMOTE_TOKEN=one-time-token",
        flatpak_desktop.APP_ID,
    ]
    assert kwargs["cwd"] == str(tmp_path)


def test_start_native_backend_uses_ephemeral_loopback_port_and_ready_file(monkeypatch, tmp_path):
    ready_file = tmp_path / "ready.json"
    spawned = []

    class FakeProcess:
        def poll(self):
            return None
        def terminate(self):
            pass
        def wait(self, timeout):
            pass

    def fake_popen(command, **kwargs):
        spawned.append((command, kwargs))
        ready_path = kwargs["env"]["HERMES_DESKTOP_READY_FILE"]
        with open(ready_path, "w", encoding="utf-8") as handle:
            json.dump({"port": 41234}, handle)
        return FakeProcess()

    monkeypatch.setattr(flatpak_desktop.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(flatpak_desktop.tempfile, "NamedTemporaryFile", lambda **kwargs: open(ready_file, "w+", encoding="utf-8"))

    process, url, token = flatpak_desktop._start_native_backend(["/native/hermes"], cwd=str(tmp_path))

    assert process is not None
    assert url == "http://127.0.0.1:41234"
    assert token
    command, kwargs = spawned[0]
    assert command == ["/native/hermes", "serve", "--isolated", "--host", "127.0.0.1", "--port", "0"]
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["env"]["HERMES_DASHBOARD_SESSION_TOKEN"] == token
    assert "HERMES_DESKTOP_READY_FILE" in kwargs["env"]


def test_desktop_snapd_builds_and_launches_without_the_local_pack(monkeypatch, tmp_path):
    monkeypatch.setattr(main_desktop.sys, "platform", "linux")
    monkeypatch.setattr(main_desktop, "_project_root", lambda: tmp_path)
    snap_file = tmp_path / "hermes-desktop.snap"
    snap_file.write_bytes(b"fake")
    built = []
    installed = []
    launched = []

    monkeypatch.setattr(snap_desktop, "build_snap", lambda root: built.append(root) or snap_file)
    monkeypatch.setattr(snap_desktop, "install_snap", lambda path: installed.append(path) or True)
    monkeypatch.setattr(
        snap_desktop,
        "launch_with_native_backend",
        lambda command, *, cwd: launched.append((command, cwd)) or 0,
    )

    with pytest.raises(SystemExit) as exc:
        main_desktop.cmd_gui(argparse.Namespace(source=False, snapd=True, flatpak=False, cwd=str(tmp_path)))

    assert exc.value.code == 0
    assert built == [tmp_path]
    assert installed == [snap_file]
    assert launched == [([sys.executable, "-m", "hermes_cli.main"], str(tmp_path))]


def test_snap_install_uses_devmode_for_the_local_unsigned_build(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(snap_desktop, "_snap", lambda: "/usr/bin/snap")
    monkeypatch.setattr(
        snap_desktop,
        "_run",
        lambda command, **kwargs: commands.append(command) or subprocess.CompletedProcess(command, 0),
    )
    snap_path = tmp_path / "hermes-desktop.snap"
    snap_path.write_bytes(b"fake")

    assert snap_desktop.install_snap(snap_path) is True
    assert commands == [["/usr/bin/snap", "install", "--dangerous", "--devmode", str(snap_path)]]