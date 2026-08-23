"""Linux Snap lifecycle for the locally-built Hermes Desktop snap.

The Snap is NOT built or published by CI — GitHub Actions only produces the
AppImage. This module supports the opt-in ``hermes desktop --snapd`` flow,
which builds the snap from the local workspace (``npm run dist:snap`` in
``apps/desktop``) and launches it against a temporary native backend. Hermes
CLI, gateway, configuration, credentials, plugins, skills, cron, and
workspace data remain in the user's normal native Hermes installation.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

SNAP_NAME = "hermes-desktop"
_READY_TIMEOUT_SECONDS = 90


def _snap() -> Optional[str]:
    return shutil.which("snap")


def _run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=False, **kwargs)


def _snap_available() -> bool:
    if _snap():
        return True
    print(
        "The --snapd flow requires snapd and the snapcraft CLI. "
        "Install them, then run `hermes desktop --snapd` again."
    )
    return False


def build_snap(project_root: Path) -> Optional[Path]:
    """Stage and build the hermes-desktop snap from the local workspace.

    Returns the path to the built .snap file, or None on failure.
    """
    desktop_dir = project_root / "apps" / "desktop"
    if not (desktop_dir / "snap" / "snapcraft.yaml").exists():
        print(f"Snapcraft manifest not found at: {desktop_dir / 'snap'}")
        return None

    npm = _run(["bash", "-lc", "command -v npm"], capture_output=True, text=True)
    if npm.returncode:
        print("Desktop snap build requires Node.js/npm, but npm was not found on PATH.")
        return None

    print("Building Hermes Desktop snap (this can take several minutes)...")
    result = _run(["npm", "run", "dist:snap"], cwd=desktop_dir)
    if result.returncode:
        print("Snap build failed.")
        return None

    candidates = sorted(desktop_dir.glob("*.snap")) + sorted(
        (desktop_dir / "release").glob("*.snap")
    ) if (desktop_dir / "release").exists() else sorted(desktop_dir.glob("*.snap"))
    if not candidates:
        print("Snap build finished but no .snap artifact was found.")
        return None
    return candidates[-1]


def install_snap(snap_path: Path) -> bool:
    """Install a locally-built snap (devmode — unsigned)."""
    snap = _snap()
    if not snap:
        _snap_available()
        return False

    print(f"Installing local snap: {snap_path}")
    # The snap is unsigned, so it needs --dangerous; confinement is devmode.
    result = _run([snap, "install", "--dangerous", "--classic", str(snap_path)])
    if result.returncode:
        print("Local snap install failed.")
        return False
    return True


def launch_with_native_backend(command: list[str], *, cwd: str) -> int:
    """Launch the installed hermes-desktop snap against a native backend."""
    snap = _snap()
    assert snap is not None

    token = secrets.token_urlsafe(32)
    ready_handle = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", prefix="hermes-desktop-ready-", delete=False
    )
    ready_path = Path(ready_handle.name)
    ready_handle.close()
    ready_path.unlink(missing_ok=True)

    env = dict(os.environ)
    env["HERMES_DASHBOARD_SESSION_TOKEN"] = token
    env["HERMES_DESKTOP_READY_FILE"] = str(ready_path)
    env["HERMES_DESKTOP"] = "1"

    process = subprocess.Popen(
        [*command, "serve", "--isolated", "--host", "127.0.0.1", "--port", "0"],
        cwd=cwd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("native Hermes backend exited before it became ready")
            try:
                payload = json.loads(ready_path.read_text(encoding="utf-8"))
                port = int(payload["port"])
                if port > 0:
                    base_url = f"http://127.0.0.1:{port}"
                    break
            except (FileNotFoundError, ValueError, KeyError, json.JSONDecodeError):
                pass
            time.sleep(0.1)
        else:
            process.terminate()
            raise RuntimeError("timed out waiting for the native Hermes backend to become ready")
    except Exception:
        try:
            process.terminate()
        except Exception:
            pass
        ready_path.unlink(missing_ok=True)
        print("Could not start the native Hermes backend.")
        return 1

    ready_path.unlink(missing_ok=True)

    try:
        result = _run(
            [
                snap,
                "run",
                f"--env=HERMES_DESKTOP_REMOTE_URL={base_url}",
                f"--env=HERMES_DESKTOP_REMOTE_TOKEN={token}",
                SNAP_NAME,
            ],
            cwd=cwd,
        )
        return result.returncode
    finally:
        if process.poll() is None:
            process.terminate()
