# Releasing

How to publish independent SDK and example-catalog releases.

An SDK release marks a validated SDK revision and publishes `palmimo-sdk` to
PyPI. An examples release publishes the official-app catalog used by Palmimo
Portal. Neither kind pushes anything to a device. If you have a clone of this
repository, update it by fetching and checking out a tag (or by pulling
`main`), not by downloading a release asset.

Palmimo Portal is a separate product, maintained in its own
repository; it self-updates from there, independently of this repository's
releases.

## 1. SDK releases

- SDK tags are `vX.Y.Z`, optionally with an `-rcN` pre-release suffix
  (`vX.Y.Z-rc1`). The release workflow rejects any other shape.
- **Never delete or move a published tag** — publish a newer tag instead of
  correcting an old one in place.
- An SDK tag with an `-rcN` suffix (e.g. `v1.2.0-rc1`) is created as a GitHub
  pre-release automatically (see [What CI does](#what-ci-does) below).
  `GET repos/{repo}/releases/latest` ignores both drafts and pre-releases,
  so a pre-release build can never become "the latest release" by accident.

### Before tagging

1. Bump `version` in `pyproject.toml` and `packages/palmimo_sdk/pyproject.toml`.
2. Regenerate both lockfiles so they record the new versions -- CI's `lock`
   and `integration-lerobot` jobs check them and fail on a bare pyproject
   bump:

   ```bash
   uv lock
   cd integrations/lerobot && uv lock
   ```

3. Put all four files (the two `pyproject.toml`s and the two `uv.lock`s) in
   one pull request. Merge it, and confirm CI is green on `main`.
4. Walk [raspberry-pi-setup.md](raspberry-pi-setup.md) end to end from a
   blank card before tagging, and record the board, image, and result in the
   release notes.
5. Tag the merged commit:

   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```

Who may do this: every member of the GitHub organization has write access
to this repository, and write access is all it takes to push a tag, run
the release workflow, and publish the resulting draft. There is no
separate release role on purpose. The workflow refuses a final tag that is
not on `main`, and the human gate is reading the draft before publishing it.

### What CI does

Pushing a `v*` tag triggers `.github/workflows/release.yml`:

1. Verifies the tag's shape, and for a final tag that the tagged commit is
   an ancestor of `main` — refuses to build a release from a tag pushed at a
   stray commit. An `-rcN` tag may point at a pull request's branch, so a
   candidate can be verified on a device before the pull request merges.
2. Creates the release as a **draft**, with GitHub's auto-generated notes
   (shaped by `.github/release.yml` — see [Labels](#3-labels-that-drive-the-notes)
   below). An `-rcN` tag is created with `--prerelease` so it can never
   surface as `releases/latest`.
Re-running the workflow for a tag that already has a release does nothing
if that release already exists — draft or published. Re-running it for a
tag whose release has already been **published** still refuses to touch it:
cut a new tag instead.

### Publish

1. Open the draft release on GitHub.
2. Review the generated notes against the [template](#release-notes-template)
   below, and paste in the hand-written top block.
3. For a real release (not a pre-release), tick **"Set as the latest
   release"**.
4. Publish.

Publishing triggers `.github/workflows/publish.yml`: it builds `palmimo-sdk`,
uploads it to TestPyPI, installs that build and smoke-tests it, and only
then uploads the same build to PyPI. A pre-release stops after the TestPyPI
smoke test — it never reaches PyPI. `workflow_dispatch` runs the same
TestPyPI-and-smoke path on demand, as a rehearsal, without a release; it
uploads `X.Y.Z.dev<run id>` rather than `X.Y.Z`, so a rehearsal never
occupies the version a later release will need on TestPyPI.

If `publish.yml` fails, re-run the failed jobs from the Actions page. PyPI
never accepts a version it already holds, so once the `pypi` upload itself
has gone through there is nothing to re-run: fix the problem and cut a new
patch version instead.

## 2. Examples catalog releases

Catalog tags are `examples-vX.Y.Z`, optionally with an `-rcN` pre-release
suffix (`examples-vX.Y.Z-rc1`). Use an examples tag for a catalog change even when
the SDK version does not change. Never delete or move a published tag; publish
a newer tag instead.

1. Merge the example and manifest changes to `main` and confirm CI is green.
2. Tag that merged commit and push the tag:

   ```bash
   git tag -a examples-vX.Y.Z -m "examples-vX.Y.Z"
   git push origin examples-vX.Y.Z
   ```

3. Open the draft release on GitHub, review its generated notes, then publish
   it. Leave "Set as the latest release" unticked: the repository's latest
   release is the SDK's, and Portal finds the catalog by its `examples-v`
   tag, not by `releases/latest`.

Pushing either kind of tag runs `.github/workflows/release.yml`, with the
same tag-shape and `main` checks as an SDK tag, and creates a draft release
with generated notes. An `-rcN` tag is marked as a GitHub pre-release. For
an `examples-v*` tag only, it also builds
`palmimo-catalog-<tag>.json` and its `.sha256` from every example manifest
(see [App Manifest](../reference/app-manifest.md)), then attaches both to the
draft. Publishing an examples release does not invoke `publish.yml`, so it
never publishes to TestPyPI or PyPI.

Palmimo Portal reads the catalog only from `examples-v` releases. Its release
selection is maintained in the Portal repository.

## 3. Labels that drive the notes

Label a pull request with one of these **before merging** so
`.github/release.yml` files it under the right heading:

| Label | Heading |
|---|---|
| `breaking-change` | Breaking changes |
| `sdk`, `motion` | SDK & motions |
| `example`, `integration` | Examples & integrations |
| `bug` | Fixes |
| `documentation` | Documentation |
| `skip-changelog` | excluded entirely |
| `dependencies` | excluded entirely |
| (none of the above) | Other changes |

## 4. Verifying a release

```bash
gh release view vX.Y.Z
gh release view examples-vX.Y.Z
```

## Release notes template

GitHub has no free-form release-template file — `.github/release.yml` only
shapes the auto-generated "What's Changed" section. Paste this hand-written
block above it when publishing:

```markdown
## Highlights

- 2-4 bullets on what this release is for

## Upgrade notes

- Anything a user must do or expect when updating a clone to this tag

## Known issues

- Anything shipped with a known gap, and its workaround if any

<!-- GitHub's generated "What's Changed" section follows below -->
```
