"""Plan and merge an upstream Hermes release into the fork packaging branch.

The scheduled workflow calls this so a new NousResearch release is either
merged cleanly or rejected before any package is published. Conflicted merges
are aborted; the caller opens an issue and stops.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# Stable upstream releases are semver (v0.22.5) or the older calver tags
# (v2026.9.24, v2026.8.16.2). Pre-release tags such as v0.22.5+canary are
# not GitHub "latest" releases and are refused so they cannot be published.
_STABLE_TAG = re.compile(
    r"^v(?:\d+\.\d+\.\d+|\d{4}\.\d{1,2}\.\d{1,2}(?:\.\d+)?)$"
)
_RELEASE_VERSION = re.compile(
    r'(<release\s+version=")[^"]*("\s+date=")[^"]*("\s*/>)'
)

PACKAGING_BRANCH = "feat/flatpak-snapcraft-appimage"
UPSTREAM_REPOSITORY = "NousResearch/hermes-agent"


@dataclass(frozen=True)
class SyncPlan:
    action: str
    tag: str
    reason: str


@dataclass(frozen=True)
class MergeResult:
    status: str
    sha: str
    conflicts: tuple[str, ...] = ()


def normalize_release_tag(tag: str) -> str:
    """Return a stable release tag, or raise ValueError."""
    cleaned = tag.strip()
    if not _STABLE_TAG.fullmatch(cleaned):
        raise ValueError(
            f"refusing tag {tag!r}; expected a stable release such as v0.22.5"
        )
    return cleaned


def version_from_tag(tag: str) -> str:
    """Strip the leading v from a tag that already passed normalize_release_tag."""
    return normalize_release_tag(tag)[1:]


def plan_sync(
    *,
    upstream_tag: str,
    fork_release_tags: list[str] | tuple[str, ...] | set[str],
    requested: str | None = None,
) -> SyncPlan:
    """Decide whether to build this upstream release.

    An existing fork release for the same tag is a skip so reruns are
    idempotent. ``requested`` overrides the upstream latest tag for a
    manual dispatch.
    """
    chosen = requested.strip() if requested and requested.strip() else upstream_tag
    tag = normalize_release_tag(chosen)
    if tag in set(fork_release_tags):
        return SyncPlan("skip", tag, "fork release already exists")
    return SyncPlan("sync", tag, "fork release missing")


def conflict_issue_title(tag: str) -> str:
    return f"Upstream packaging sync conflict: {tag}"


def release_notes(tag: str, sha: str) -> str:
    """Release body for the fork's Linux desktop packages."""
    tag = normalize_release_tag(tag)
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"invalid commit sha {sha!r}")
    version = version_from_tag(tag)
    return (
        f"Linux desktop packages for Hermes Agent {tag} (x86_64).\n\n"
        f"Built from NousResearch/hermes-agent@{tag} plus this fork's desktop packaging on `{sha}`.\n\n"
        f"- `Hermes-{version}-linux-x64.AppImage` — `chmod +x` and run\n"
        f"- `Hermes-{version}-linux-x64.deb` — `sudo apt install ./Hermes-{version}-linux-x64.deb`\n"
        f"- `Hermes-{version}-linux-x64.rpm` — `sudo dnf install ./Hermes-{version}-linux-x64.rpm`\n"
        f"- `Hermes-{version}-linux-x64.flatpak` — `flatpak install --user ./*.flatpak`\n"
        "- Snap — `sudo snap install --dangerous --devmode ./hermes-desktop_*.snap`\n\n"
        f"Upstream release: https://github.com/NousResearch/hermes-agent/releases/tag/{tag}\n"
    )


def conflict_issue_body(tag: str, files: list[str] | tuple[str, ...]) -> str:
    listing = "\n".join(f"- `{path}`" for path in files) or "- (git reported a conflict but named no files)"
    return (
        f"Upstream `{tag}` did not merge cleanly into `{PACKAGING_BRANCH}`.\n\n"
        "No Linux desktop packages were published for this version.\n\n"
        "Conflicted files:\n"
        f"{listing}\n\n"
        f"Resolve the conflicts on `{PACKAGING_BRANCH}`, push the merge, and "
        "re-run the Sync upstream Linux release workflow "
        "(`workflow_dispatch`). The workflow will not publish a partial build.\n"
    )


def stamp_package_version(desktop_dir: Path, version: str, release_date: str) -> None:
    """Write the release version into Snap and Flatpak metadata.

    Electron AppImage/deb/rpm versions are passed to electron-builder
    separately. These files are what Flatpak and Snap embed.
    """
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:\.\d+)?", version):
        raise ValueError(f"invalid package version {version!r}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", release_date):
        raise ValueError(f"invalid release date {release_date!r}")

    snapcraft = desktop_dir / "snap" / "snapcraft.yaml"
    _rewrite(
        snapcraft,
        lambda text: re.sub(
            r"(?m)^version:\s*.*$",
            f"version: '{version}'",
            text,
            count=1,
        ),
    )
    replacement = rf"\g<1>{version}\g<2>{release_date}\g<3>"
    for relative in (
        Path("flatpak") / "com.nousresearch.Hermes.metainfo.xml",
        Path("snap") / "gui" / "hermes-desktop.metainfo.xml",
    ):
        path = desktop_dir / relative
        _rewrite(path, lambda text, replacement=replacement: _RELEASE_VERSION.sub(replacement, text, count=1))


def _rewrite(path: Path, transform) -> None:
    original = path.read_text(encoding="utf-8")
    updated = transform(original)
    if updated == original:
        raise ValueError(f"version stamp did not change {path}")
    path.write_text(updated, encoding="utf-8")


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
    )


def _ensure_git_identity(repo: Path) -> None:
    for key, value in (
        ("user.email", "github-actions[bot]@users.noreply.github.com"),
        ("user.name", "github-actions[bot]"),
    ):
        current = _git(repo, "config", "--get", key, check=False)
        if current.returncode != 0 or not current.stdout.strip():
            _git(repo, "config", key, value)


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def merge_upstream_tag(repo: Path, tag: str) -> MergeResult:
    """Merge ``tag`` into the current checkout.

    ``up_to_date`` and ``merged`` are publishable. ``conflict`` aborts the
    merge and leaves the checkout on the pre-merge commit.
    """
    tag = normalize_release_tag(tag)
    _ensure_git_identity(repo)
    before = _head(repo)
    merged = _git(
        repo,
        "merge",
        "--no-ff",
        "-m",
        f"merge upstream {tag} into Linux packaging",
        tag,
        check=False,
    )
    if merged.returncode == 0:
        sha = _head(repo)
        if sha == before or "Already up to date" in merged.stdout:
            return MergeResult("up_to_date", sha)
        return MergeResult("merged", sha)

    conflicts = _git(repo, "diff", "--name-only", "--diff-filter=U", check=False)
    names = tuple(line for line in conflicts.stdout.splitlines() if line.strip())
    if _git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
        abort = _git(repo, "merge", "--abort", check=False)
        if abort.returncode != 0:
            raise RuntimeError(abort.stderr.strip() or "git merge --abort failed")
    if _head(repo) != before:
        raise RuntimeError("merge failed and the checkout did not return to the pre-merge commit")
    if not names and "CONFLICT" not in merged.stdout and "CONFLICT" not in merged.stderr:
        detail = (merged.stderr or merged.stdout).strip()
        raise RuntimeError(detail or f"git merge {tag} failed")
    return MergeResult("conflict", before, names)


def _print_json(payload: dict) -> None:
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")


def _cmd_plan(args: argparse.Namespace) -> int:
    requested = args.requested or None
    plan = plan_sync(
        upstream_tag=args.upstream_tag,
        fork_release_tags=args.fork_tag,
        requested=requested,
    )
    _print_json(asdict(plan))
    return 0


def _cmd_merge(args: argparse.Namespace) -> int:
    result = merge_upstream_tag(Path(args.repo), args.tag)
    _print_json(
        {
            "status": result.status,
            "sha": result.sha,
            "conflicts": list(result.conflicts),
        }
    )
    if result.status == "conflict":
        return 2
    return 0


def _cmd_stamp(args: argparse.Namespace) -> int:
    stamp_package_version(Path(args.desktop), version_from_tag(args.tag), args.date)
    return 0


def _cmd_notes(args: argparse.Namespace) -> int:
    sys.stdout.write(release_notes(args.tag, args.sha))
    return 0


def _cmd_issue_body(args: argparse.Namespace) -> int:
    files = [line for line in sys.stdin.read().splitlines() if line.strip()]
    sys.stdout.write(conflict_issue_body(normalize_release_tag(args.tag), files))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan")
    plan.add_argument("--upstream-tag", required=True)
    plan.add_argument("--fork-tag", action="append", default=[])
    plan.add_argument("--requested", default="")
    plan.set_defaults(func=_cmd_plan)

    merge = sub.add_parser("merge")
    merge.add_argument("--repo", required=True)
    merge.add_argument("--tag", required=True)
    merge.set_defaults(func=_cmd_merge)

    stamp = sub.add_parser("stamp")
    stamp.add_argument("--desktop", required=True)
    stamp.add_argument("--tag", required=True)
    stamp.add_argument("--date", required=True)
    stamp.set_defaults(func=_cmd_stamp)

    notes = sub.add_parser("notes")
    notes.add_argument("--tag", required=True)
    notes.add_argument("--sha", required=True)
    notes.set_defaults(func=_cmd_notes)

    issue = sub.add_parser("issue-body")
    issue.add_argument("--tag", required=True)
    issue.set_defaults(func=_cmd_issue_body)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
