#!/usr/bin/env bash
# Classifies a release tag for release.yml: accepts vX.Y.Z and
# examples-vX.Y.Z, either optionally with an -rcN suffix (see
# doc/guides/releasing.md). Prints "release" or "prerelease" to stdout and
# exits 0 for an accepted tag; prints an error to stderr and exits 1
# otherwise. Extracted so the classification is testable as behavior rather
# than as workflow-YAML wording.
set -euo pipefail

tag="${1:?usage: classify_release_tag.sh TAG}"

# The `examples-` prefix itself contains a hyphen, so a pre-release is
# recognized by its exact `-rcN` suffix, never by "has a hyphen".
if [[ ! "$tag" =~ ^(v|examples-v)[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?$ ]]; then
  echo "tag $tag is not vX.Y.Z or examples-vX.Y.Z, optionally with -rcN" >&2
  exit 1
fi

if [[ "$tag" =~ -rc[0-9]+$ ]]; then
  echo "prerelease"
else
  echo "release"
fi
