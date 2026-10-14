"""Release rules (RELEASING.md), checked on the repository itself."""

from __future__ import annotations

import datetime
import importlib.util
import json
import pathlib
import re
import shutil
import tomllib

import pytest

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
    assert f"ZIP: {hacs['filename']}" in workflow


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
