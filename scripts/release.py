"""Release helper (RELEASING.md). Standard library only: CI runs it with the runner's python3.

    python3 scripts/release.py 0.2.0          bump the version everywhere; date the changelog
    python3 scripts/release.py --check v0.2.0 the tag, the version files and the changelog agree
    python3 scripts/release.py --notes v0.2.0 print the changelog section of that version
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
REPO = "https://github.com/ohmie-energy/ha-ohmie"
SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(alpha|beta|rc)\.(\d+))?")
STAGE = {"alpha": 0, "beta": 1, "rc": 2, None: 3}


class ReleaseError(Exception):
    pass


def key(version: str) -> tuple[int, ...]:
    m = SEMVER.fullmatch(version)
    if not m:
        raise ReleaseError(f"{version!r} is not a version such as 1.2.3 or 1.2.3-beta.1")
    major, minor, patch, stage, n = m.groups()
    return int(major), int(minor), int(patch), STAGE[stage], int(n or 0)


def _files(root: pathlib.Path) -> dict[str, pathlib.Path]:
    return {"manifest": root / "custom_components" / "ohmie" / "manifest.json",
            "pyproject": root / "pyproject.toml", "lock": root / "uv.lock",
            "changelog": root / "CHANGELOG.md"}


def versions(root: pathlib.Path = ROOT) -> dict[str, str]:
    f = _files(root)
    lock = re.search(r'name = "ha-ohmie"\nversion = "([^"]+)"', f["lock"].read_text())
    py = re.search(r'^version = "([^"]+)"', f["pyproject"].read_text(), re.M)
    if not lock or not py:
        raise ReleaseError("no version in uv.lock or pyproject.toml")
    return {"manifest": json.loads(f["manifest"].read_text())["version"],
            "pyproject": py[1], "lock": lock[1]}


def section(changelog: str, version: str) -> str:
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]: |\Z)",
                  changelog, re.M | re.S)
    if not m:
        raise ReleaseError(f"CHANGELOG.md has no section for {version}")
    return m[1].strip()


def bump(version: str, root: pathlib.Path = ROOT, today: datetime.date | None = None) -> None:
    current = versions(root)
    if len(set(current.values())) != 1:
        raise ReleaseError(f"the version files disagree: {current}")
    old = current["manifest"]
    if key(version) <= key(old):
        raise ReleaseError(f"{version} is not newer than {old}")
    f = _files(root)
    log = f["changelog"].read_text()
    if not section(log, "Unreleased"):
        raise ReleaseError("the Unreleased section of CHANGELOG.md is empty: nothing to release")
    date = (today or datetime.date.today()).isoformat()
    log = log.replace("## [Unreleased]\n", f"## [Unreleased]\n\n## [{version}] - {date}\n", 1)
    log = re.sub(r"^\[Unreleased\]: .*$",
                 f"[Unreleased]: {REPO}/compare/v{version}...HEAD\n"
                 f"[{version}]: {REPO}/compare/v{old}...v{version}", log, count=1, flags=re.M)
    f["changelog"].write_text(log)
    f["manifest"].write_text(re.sub(r'("version":\s*)"[^"]+"', rf'\g<1>"{version}"',
                                    f["manifest"].read_text(), count=1))
    f["pyproject"].write_text(re.sub(r'^version = "[^"]+"', f'version = "{version}"',
                                     f["pyproject"].read_text(), count=1, flags=re.M))
    f["lock"].write_text(re.sub(r'(name = "ha-ohmie"\nversion = )"[^"]+"', rf'\g<1>"{version}"',
                                f["lock"].read_text(), count=1))


def check(tag: str, root: pathlib.Path = ROOT) -> str:
    found = versions(root)
    if len(set(found.values())) != 1:
        raise ReleaseError(f"the version files disagree: {found}")
    version = found["manifest"]
    if tag != f"v{version}":
        raise ReleaseError(f"the tag {tag} does not match the version {version}")
    key(version)
    if not section(_files(root)["changelog"].read_text(), version):
        raise ReleaseError(f"the CHANGELOG.md section for {version} is empty")
    return version


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("version", nargs="?")
    g.add_argument("--check", metavar="TAG")
    g.add_argument("--notes", metavar="TAG")
    a = p.parse_args(argv)
    try:
        if a.check:
            print(f"ok: {check(a.check)}")
        elif a.notes:
            print(section(_files(ROOT)["changelog"].read_text(), check(a.notes)))
        else:
            bump(a.version)
            v = a.version
            print(f"Bumped to {v}. Review the diff, then:\n"
                  f"  git commit -am 'Release {v}'\n"
                  f"  git push -u origin HEAD && gh pr create --fill\n"
                  f"After the PR is merged:\n"
                  f"  git switch main && git pull\n"
                  f"  git tag -a v{v} -m 'Ohmie for Home Assistant {v}'\n"
                  f"  git push origin v{v}")
    except ReleaseError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
