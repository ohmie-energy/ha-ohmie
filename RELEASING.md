# Releasing

Members install Ohmie from HACS, and HACS installs **GitHub Releases** only: the `ohmie.zip` that
`release.yml` builds, attests and attaches. A commit on `main` reaches nobody until it is released.

## Versions

[Semantic Versioning](https://semver.org): `MAJOR.MINOR.PATCH`, with pre-releases as
`1.2.0-beta.1` (also `alpha.N` and `rc.N`).

| Change | Bump |
|---|---|
| A fix with no change to entities, options or the config entry | patch |
| A new entity, option or blueprint; a higher minimum Home Assistant version | minor |
| A removed or renamed entity, or a config entry that needs a migration | major (minor while 0.x) |

One version lives in four places: `manifest.json`, `pyproject.toml`, `uv.lock` and the newest
section of `CHANGELOG.md`. `tests/test_release.py` fails when they disagree.

## Every change

Add a line to the `## [Unreleased]` section of `CHANGELOG.md`, in words a member understands.

## Release

`main` takes changes only through pull requests with green checks, so a release is a PR, then a tag.

1. Make sure `main` is green, then bump on a branch:

   ```bash
   git switch main && git pull && git switch -c release/0.2.0
   python3 scripts/release.py 0.2.0
   ```

2. Review the diff. Commit, push and open the PR (the script prints these commands):

   ```bash
   git commit -am "Release 0.2.0"
   git push -u origin release/0.2.0
   gh pr create --fill
   ```

3. Merge the PR when its checks are green. Then tag the merged commit on `main`:

   ```bash
   git switch main && git pull
   git tag -a v0.2.0 -m "Ohmie for Home Assistant 0.2.0"
   git push origin v0.2.0
   ```

4. `release.yml` then:
   - runs every check in `validate.yml` again;
   - refuses a tag that is not on `main`, or that does not match the version files and the changelog;
   - builds `ohmie.zip`, attests its build provenance, and publishes the release with the changelog
     section as its notes. A tag with `-` (for example `v0.2.0-beta.1`) becomes a pre-release, which
     HACS offers only to members who turn on beta versions.

Check a downloaded zip against its provenance:

```bash
gh attestation verify ohmie.zip --repo ohmie-energy/ha-ohmie
```

## Rules

- Never move, change or delete a published tag or release. A bad release is fixed by the next
  patch release; add a note to the bad release's notes.
- Do not release from a branch other than `main`.
- Keep `manifest.json`'s `requirements` the same as the pins in `pyproject.toml`
  (`tests/test_release.py` checks it): Dependabot bumps only `pyproject.toml`.

## Repository settings that protect releases

- Ruleset `main`: changes only through a pull request with green `hassfest`, `hacs`, `workflows`
  and `test`; no force push, no deletion, linear history.
- Ruleset `release tags`: a `v*` tag cannot be moved or deleted.
- Immutable releases: a published release's tag and assets cannot change.
- Environment `release`: only `v*` tags can run the publish job.
- Secret scanning with push protection, Dependabot alerts and security updates, CodeQL.
- Actions: the default token is read-only; each job asks for what it needs.
