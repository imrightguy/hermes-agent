"""Behavior tests for the upstream release sync used by the Linux packager."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.releases.upstream_linux_sync import (
    conflict_issue_body,
    conflict_issue_title,
    merge_upstream_tag,
    plan_sync,
    release_notes,
    stamp_package_version,
    version_from_tag,
)


def test_plan_skips_when_the_fork_release_exists():
    plan = plan_sync(upstream_tag="v0.22.5", fork_release_tags=["v0.22.5", "ci-test-1"])
    assert plan.action == "skip"
    assert plan.tag == "v0.22.5"


def test_plan_syncs_a_missing_release_and_honors_a_manual_tag():
    latest = plan_sync(upstream_tag="v0.22.5", fork_release_tags=["v0.22.4"])
    assert latest.action == "sync"
    assert latest.tag == "v0.22.5"

    manual = plan_sync(
        upstream_tag="v0.22.5",
        fork_release_tags=[],
        requested="v0.22.4",
    )
    assert manual.action == "sync"
    assert manual.tag == "v0.22.4"


def test_plan_refuses_prerelease_tags():
    with pytest.raises(ValueError):
        plan_sync(upstream_tag="v0.22.5+canary.20261030T064851Z", fork_release_tags=[])


def test_plan_accepts_historical_calver_release_tags():
    plan = plan_sync(upstream_tag="v2026.8.16.2", fork_release_tags=[])
    assert plan.action == "sync"
    assert version_from_tag(plan.tag) == "2026.8.16.2"


def test_release_notes_name_the_tag_and_commit():
    notes = release_notes("v0.22.5", "a" * 40)
    assert "Hermes Agent v0.22.5" in notes
    assert "Hermes-0.22.5-linux-x64.AppImage" in notes
    assert "a" * 40 in notes


def test_conflict_issue_names_the_tag_and_files():
    title = conflict_issue_title("v0.22.5")
    body = conflict_issue_body("v0.22.5", ["apps/desktop/package.json"])
    assert title == "Upstream packaging sync conflict: v0.22.5"
    assert "`apps/desktop/package.json`" in body
    assert "No Linux desktop packages were published" in body


def test_stamp_rewrites_snap_and_flatpak_versions(tmp_path: Path):
    desktop = tmp_path / "desktop"
    (desktop / "snap" / "gui").mkdir(parents=True)
    (desktop / "flatpak").mkdir()
    (desktop / "snap" / "snapcraft.yaml").write_text("name: hermes\nversion: '0.0.0'\nbase: core24\n", encoding="utf-8")
    release = '<release version="0.0.0" date="2026-08-31"/>'
    for path in (
        desktop / "flatpak" / "com.nousresearch.Hermes.metainfo.xml",
        desktop / "snap" / "gui" / "hermes-desktop.metainfo.xml",
    ):
        path.write_text(f"<releases>\n    {release}\n</releases>\n", encoding="utf-8")

    stamp_package_version(desktop, "0.22.5", "2026-10-23")

    assert "version: '0.22.5'" in (desktop / "snap" / "snapcraft.yaml").read_text(encoding="utf-8")
    for path in (
        desktop / "flatpak" / "com.nousresearch.Hermes.metainfo.xml",
        desktop / "snap" / "gui" / "hermes-desktop.metainfo.xml",
    ):
        text = path.read_text(encoding="utf-8")
        assert 'version="0.22.5"' in text
        assert 'date="2026-10-23"' in text


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir()
    _git(path, "init", "-b", "packaging")
    _git(path, "config", "user.email", "sync-test@example.com")
    _git(path, "config", "user.name", "sync-test")


def _commit(repo: Path, relative: str, content: str, message: str) -> None:
    file_path = repo / relative
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")
    _git(repo, "add", relative)
    _git(repo, "commit", "-m", message)


def test_merge_upstream_tag_merges_when_clean_and_aborts_on_conflict(tmp_path: Path):
    upstream = tmp_path / "upstream"
    fork = tmp_path / "fork"
    _init_repo(upstream)
    _commit(upstream, "README.md", "base\n", "base")
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=upstream, text=True).strip()

    _init_repo(fork)
    _git(fork, "fetch", str(upstream), base)
    _git(fork, "merge", "--ff-only", "FETCH_HEAD")
    _commit(fork, "packaging.txt", "overlay\n", "packaging")

    _commit(upstream, "README.md", "upstream\n", "upstream change")
    _git(upstream, "tag", "v0.22.5")
    _git(fork, "fetch", str(upstream), "tag", "v0.22.5")

    merged = merge_upstream_tag(fork, "v0.22.5")
    assert merged.status == "merged"
    readme = (fork / "README.md").read_text(encoding="utf-8")
    assert readme == "upstream\n"
    assert (fork / "packaging.txt").read_text(encoding="utf-8") == "overlay\n"

    _commit(fork, "README.md", "fork edit\n", "fork edit")
    _commit(upstream, "README.md", "upstream again\n", "upstream again")
    subprocess.run(["git", "tag", "-d", "v0.22.6"], cwd=upstream, check=False, capture_output=True)
    _git(upstream, "tag", "v0.22.6")
    _git(fork, "fetch", str(upstream), "tag", "v0.22.6")
    before = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=fork, text=True).strip()

    conflict = merge_upstream_tag(fork, "v0.22.6")
    assert conflict.status == "conflict"
    assert conflict.conflicts == ("README.md",)
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=fork, text=True).strip() == before
    assert not (fork / ".git" / "MERGE_HEAD").exists()
    assert (fork / "README.md").read_text(encoding="utf-8") == "fork edit\n"


def test_merge_is_up_to_date_when_the_tag_is_already_contained(tmp_path: Path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    _commit(repo, "README.md", "base\n", "base")
    _git(repo, "tag", "v0.22.5")

    result = merge_upstream_tag(repo, "v0.22.5")
    assert result.status == "up_to_date"
    assert result.sha == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()


def test_cli_plan_prints_json(tmp_path: Path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "releases" / "upstream_linux_sync.py"
    completed = subprocess.run(
        [
            "python3",
            str(script),
            "plan",
            "--upstream-tag",
            "v0.22.5",
            "--fork-tag",
            "v0.22.4",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    assert payload["action"] == "sync"
    assert payload["tag"] == "v0.22.5"
