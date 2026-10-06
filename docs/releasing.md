# Releasing

How a version of Pyrrhula is cut, and how a fix ships while the next feature set is still
being built.

## The two lines

| Branch | Carries | Version in `pyproject.toml` |
|---|---|---|
| `main` | the next feature release | `X.Y.0.devN`, for example `0.2.0.dev0` |
| `release/X.Y` | fixes to the current release line | the last release, then the next patch |

A feature release (`0.2.0`) is tagged on `main`. When it ships, `release/0.2` is branched
from its tag and `main` moves to `0.3.0.dev0`. A patch release (`0.1.2`) is tagged on
`release/0.1`. CI runs on both kinds of branch, and the release workflow refuses any tag
whose commit has no green CI run.

## A fix that must ship before the next feature release

1. Fix it on `main` first, through a pull request, with its test. That way the fix can never
   be missing from the next feature release.
2. Cherry-pick the merge onto `release/X.Y` through a second pull request
   (`git cherry-pick -x <sha>`). Use `-x` so the commit names its origin.
3. On `release/X.Y`, cut the patch (below).

If the fix only applies to the old line (`main` changed the code since), make it on
`release/X.Y` directly and say so in that pull request.

## Cutting a release

1. On the branch that will be tagged, in a pull request:
   - Run `scripts/set_version.py X.Y.Z`. It sets the source version (`pyproject.toml`,
     `uv.lock`, `web/package.json`) and moves every published-image reference: the release
     compose file's default tag, the release installer's fallback, and the install docs'
     examples.
   - In `CHANGELOG.md`, move *Unreleased* under `## [X.Y.Z] - YYYY-MM-DD` and add its
     link at the bottom.
   - Update the status line in `README.md` and `FAQ.md`.
2. Merge, then wait for CI on the merge commit to **finish green**. A tag pushed earlier
   fails the guard in about nine seconds. If that happens, re-run the release once CI is
   green:

   ```bash
   gh workflow run release.yml --ref <branch> -f tag=vX.Y.Z
   ```

3. Tag the merge commit and push the tag:

   ```bash
   git tag -a vX.Y.Z -m "Pyrrhula X.Y.Z" && git push origin vX.Y.Z
   ```

   The release workflow builds and pushes both images and creates the GitHub release with
   the compose file attached.
4. Only the highest final version is marked *latest*, which is what the one-line installer
   follows. So a `0.1.3` cut after `0.2.0` does not take over new installs.
5. Check that the images pull anonymously, and that the release's one-line install works
   on a clean machine.

## After a feature release

- Branch `release/X.Y` from the tag: `git push origin vX.Y.0^{commit}:refs/heads/release/X.Y`.
- On `main`, run `scripts/set_version.py X.(Y+1).0.dev0` in a pull request. A dev version
  moves only the source version; published references stay on the release people can pull
  (`tests/architecture/test_compose_release_parity.py` checks both rules). Note in the
  README status line that `main` is ahead of the latest release.

## Pre-releases

`scripts/set_version.py X.Y.ZrcN` gives image tag `X.Y.Z-rcN`. Tag `vX.Y.Z-rcN`; the
release is marked as a pre-release, and the installer's *latest* lookup skips it.
