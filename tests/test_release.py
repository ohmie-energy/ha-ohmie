"""Release rules (RELEASING.md), checked on the repository itself."""

from __future__ import annotations

import datetime
import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess
import tomllib
import zipfile

import pytest
import yaml

ROOT = pathlib.Path(__file__).parents[1]
_spec = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")
assert _spec and _spec.loader
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)
SRC = ROOT / "custom_components" / "ohmie"
SEMVER = r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-(alpha|beta|rc)\.\d+)?"


def manifest_version() -> str:
    return json.loads((SRC / "manifest.json").read_text())["version"]


def test_the_version_is_semver():
    assert re.fullmatch(SEMVER, manifest_version())


def test_one_version_everywhere():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    lock = re.search(r'name = "ha-ohmie"\nversion = "([^"]+)"', (ROOT / "uv.lock").read_text())
    assert lock
    assert {pyproject, lock[1]} == {manifest_version()}


def test_the_changelog_has_the_current_version_and_an_unreleased_section():
    text = (ROOT / "CHANGELOG.md").read_text()
    headings = re.findall(r"^## \[([^\]]+)\]", text, re.M)
    assert headings[0] == "Unreleased"
    assert headings[1] == manifest_version()  # the newest release is the current version


def test_hacs_installs_the_zip_the_release_workflow_builds():
    hacs = json.loads((ROOT / "hacs.json").read_text())
    assert hacs["zip_release"] is True
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert f"ASSET: {hacs['filename']}" in workflow
    assert 'release.py --build "$ASSET"' in workflow  # the build CI runs is the one tested here


def test_the_english_translation_is_the_source_strings():
    # Home Assistant shows translations/en.json; strings.json is what we edit. They must not drift.
    assert json.loads((SRC / "translations" / "en.json").read_text()) == json.loads(
        (SRC / "strings.json").read_text())


# --- scripts/release.py ---


@pytest.fixture
def repo(tmp_path):
    for rel in ("custom_components/ohmie/manifest.json", "pyproject.toml", "uv.lock",
                "CHANGELOG.md"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / rel, tmp_path / rel)
    return tmp_path


def unreleased(repo, text="### Fixed\n\n- A fix.\n"):
    log = repo / "CHANGELOG.md"
    log.write_text(log.read_text().replace("## [Unreleased]\n", f"## [Unreleased]\n\n{text}", 1))


def test_a_bump_moves_every_version_and_dates_the_changelog(repo):
    old = manifest_version()
    unreleased(repo)
    release.bump("9.8.7", repo, today=datetime.date(2027, 1, 2))
    assert set(release.versions(repo).values()) == {"9.8.7"}
    log = (repo / "CHANGELOG.md").read_text()
    assert "## [Unreleased]\n\n## [9.8.7] - 2027-01-02\n\n### Fixed" in log
    assert release.section(log, "9.8.7") == "### Fixed\n\n- A fix."
    assert release.section(log, "Unreleased") == ""
    assert f"[9.8.7]: {release.REPO}/compare/v{old}...v9.8.7" in log
    assert release.check("v9.8.7", repo) == "9.8.7"


@pytest.mark.parametrize("version, why", [
    ("0.0.1", "not newer"), ("1.2", "not a version"), ("v1.2.3", "not a version")])
def test_a_bump_refuses_a_bad_version(repo, version, why):
    unreleased(repo)
    with pytest.raises(release.ReleaseError, match=why):
        release.bump(version, repo)


def test_a_bump_refuses_an_empty_unreleased_section(repo):
    with pytest.raises(release.ReleaseError, match="nothing to release"):
        release.bump("9.0.0", repo)


def test_pre_releases_sort_before_the_release():
    assert release.key("1.0.0-alpha.2") < release.key("1.0.0-beta.1") < release.key(
        "1.0.0-rc.1") < release.key("1.0.0") < release.key("1.0.1")


def test_check_refuses_a_tag_that_is_not_the_version(repo):
    with pytest.raises(release.ReleaseError, match="does not match"):
        release.check("v9.9.9", repo)
    assert release.check(f"v{manifest_version()}", repo) == manifest_version()


def test_the_manifest_pins_what_the_tests_run():
    # Home Assistant installs manifest.json's requirements; the tests run pyproject's dev pins.
    # Dependabot bumps only pyproject, so a bump that skips the manifest fails here.
    manifest = dict(r.split("==") for r in json.loads((SRC / "manifest.json").read_text())[
        "requirements"])
    dev = dict(re.match(r"([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?==(.+)", d).groups()
               for d in tomllib.loads((ROOT / "pyproject.toml").read_text())["dependency-groups"][
                   "dev"] if "==" in d)
    assert {k: dev.get(k) for k in manifest} == manifest


def tracked_integration_files() -> set[str]:
    out = subprocess.run(["git", "ls-files", "custom_components/ohmie"], cwd=ROOT, check=True,
                         capture_output=True, text=True).stdout.split()
    return {pathlib.PurePosixPath(p).relative_to("custom_components/ohmie").as_posix() for p in out}


def test_the_release_zip_holds_exactly_the_integration_at_its_root(tmp_path):
    src = tmp_path / "ohmie"
    shutil.copytree(SRC, src, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "stray.cpython-314.pyc").write_bytes(b"x")  # never shipped
    (src / ".DS_Store").write_bytes(b"x")
    digest = release.build(tmp_path / "ohmie.zip", src)
    with zipfile.ZipFile(tmp_path / "ohmie.zip") as z:
        names = set(z.namelist())
        assert z.testzip() is None
    assert names == tracked_integration_files()
    assert {"manifest.json", "brand/icon.png", "translations/en.json"} <= names
    assert digest == hashlib.sha256((tmp_path / "ohmie.zip").read_bytes()).hexdigest()


def test_the_release_zip_is_reproducible(tmp_path):
    assert release.build(tmp_path / "a.zip") == release.build(tmp_path / "b.zip")


def test_the_build_ignores_tool_option_variables(tmp_path, monkeypatch):
    # v0.1.0 failed because Info-ZIP reads $ZIP as options. The build uses no external tool.
    for var in ("ZIP", "ZIPOPT", "UNZIP", "GZIP", "TAR_OPTIONS"):
        monkeypatch.setenv(var, "ohmie.zip -q")
    assert release.build(tmp_path / "ohmie.zip") == release.build(tmp_path / "again.zip")


@pytest.mark.parametrize("workflow", ["release.yml", "validate.yml"])
def test_workflow_shell_steps_fail_on_a_failed_pipe(workflow):
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / workflow).read_text())
    assert wf["defaults"]["run"]["shell"] == "bash"  # GitHub then runs bash -eo pipefail


def test_the_release_job_steps_run_here(tmp_path):
    # Runs release.yml's own shell steps, as CI does, with gh stubbed: the publish step's
    # arguments, the notes and the built files are checked without a tag or a network.
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    job = wf["jobs"]["release"]
    steps = {s["name"]: s["run"] for s in job["steps"] if "run" in s}
    work = tmp_path / "work"
    shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(
        ".git", ".venv", "__pycache__", "*.pyc", ".*_cache", ".idea", ".DS_Store"))
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "gh").write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$GH_ARGS"\n')
    (stub / "gh").chmod(0o755)
    tag = f"v{manifest_version()}"
    env = {**os.environ, **{k: str(v) for k, v in job["env"].items() if "${{" not in str(v)},
           "TAG": tag, "GITHUB_WORKSPACE": str(work), "GH_ARGS": str(tmp_path / "gh.args"),
           "PATH": f"{stub}{os.pathsep}{os.environ['PATH']}",
           "ZIP": "ohmie.zip -q", "ZIPOPT": "-q"}  # what broke v0.1.0 must not matter
    for name in ("The tag, the version files and the changelog agree",
                 "Build the zip that HACS installs", "Publish the release"):
        subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", steps[name]],
                       cwd=work, env=env, check=True, capture_output=True)
    asset = job["env"]["ASSET"]
    digest, name = (work / f"{asset}.sha256").read_text().split()
    assert (name, digest) == (asset, hashlib.sha256((work / asset).read_bytes()).hexdigest())
    args = (tmp_path / "gh.args").read_text().split("\n")
    assert args[:5] == ["release", "create", tag, asset, f"{asset}.sha256"]
    assert {"--verify-tag", "--notes-file"} <= set(args)
    log = (ROOT / "CHANGELOG.md").read_text()
    assert (work / "notes.md").read_text().strip() == release.section(log, manifest_version())
