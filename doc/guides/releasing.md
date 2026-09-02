# Releasing

How to cut a palmimo-devkit release: a SemVer tag and one GitHub Release.

A release marks a validated SDK revision with human-readable notes — it
carries no asset, and it does not push anything to any device. If you have
a clone of this repository, you update by fetching and checking out the
tag (or by pulling `main`), not by downloading anything from the release
page.

Palmimo Portal is a separate product, maintained in its own
repository; it self-updates from there, independently of this repository's
releases.

## 1. Versioning

- Tags are SemVer: `vX.Y.Z`, optionally with a pre-release suffix like
  `vX.Y.Z-rc1`.
- One tag = one GitHub Release.
- **Never delete or move a published tag** — publish a newer tag instead of
  correcting an old one in place.
- A tag with a `-` suffix (e.g. `v1.2.0-rc1`) is created as a GitHub
  pre-release automatically (see [What CI does](#3-what-ci-does) below).
  `GET repos/{repo}/releases/latest` ignores both drafts and pre-releases,
  so a pre-release build can never become "the latest release" by accident.

## 2. Before tagging

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
separate release role on purpose. The workflow refuses a tag that is not
on `main`, and the human gate is section 4 (Publish) below — reading the draft before
publishing it.

## 3. What CI does

Pushing a `v*` tag triggers `.github/workflows/release.yml`:

1. Verifies the tagged commit is actually an ancestor of `main` — refuses to
   build a release from a tag pushed at a stray commit.
2. Creates the release as a **draft**, with GitHub's auto-generated notes
   (shaped by `.github/release.yml` — see [Labels](#5-labels-that-drive-the-notes)
   below). If the tag name contains a `-` (a pre-release build), the release
   is created with `--prerelease` so it can never surface as
   `releases/latest`.

Re-running the workflow for a tag that already has a release does nothing
if that release already exists — draft or published. Re-running it for a
tag whose release has already been **published** still refuses to touch it:
cut a new tag instead.

## 4. Publish

1. Open the draft release on GitHub.
2. Review the generated notes against the [template](#release-notes-template)
   below, and paste in the hand-written top block.
3. For a real release (not a pre-release), tick **"Set as the latest
   release"**.
4. Publish.

## 5. Labels that drive the notes

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

## 6. Verifying a release

```bash
gh release view vX.Y.Z
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
