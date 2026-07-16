#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VERSION_FILES=(
  packages/anywidget-mcp/pyproject.toml
  uv.lock
)

usage() {
  cat <<'EOF'
Usage: ./scripts/release.sh [major|minor|patch|stable|alpha|beta|rc|X.Y.Z]

With no argument, release the current package version. A bump or explicit PEP
440 version updates the Python package with uv. The script validates the full
workspace, creates any needed release commit, and adds an annotated v<version>
tag locally.

Start a release-candidate series with an explicit version such as 0.0.1rc1.
Use rc for the next candidate and stable for the final version.
EOF
}

die() {
  printf 'Error: %s\n' "$1" >&2
  exit 1
}

step() {
  printf '\n==> %s\n' "$1"
}

confirm() {
  local reply
  printf '%s [y/N] ' "$1"
  read -r reply
  [[ "$reply" == "y" || "$reply" == "yes" ]]
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Missing required command: $1"
}

restore_version() {
  local status=$?
  if [[ "$status" -ne 0 && "${VERSION_UPDATED:-0}" == "1" && "${COMMITTED:-0}" == "0" ]]; then
    uv version --package anywidget-mcp --no-sync "$CURRENT_VERSION" >/dev/null
    printf '\nRestored the package version to %s.\n' "$CURRENT_VERSION" >&2
  fi
  exit "$status"
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi
if [[ "$#" -gt 1 ]]; then
  usage >&2
  exit 1
fi

for command in git make pnpm uv; do
  require_command "$command"
done

[[ "$(git branch --show-current)" == "main" ]] || die "Releases must run from main"
[[ -z "$(git status --porcelain)" ]] || die "Git working directory must be clean"

step "Updating main"
git fetch origin main --tags
git pull --ff-only origin main

CURRENT_VERSION="$(uv version --package anywidget-mcp --short)"
REQUEST="${1:-}"
VERSION_ARGS=()
case "$REQUEST" in
  "")
    NEW_VERSION="$CURRENT_VERSION"
    ;;
  major | minor | patch | stable | alpha | beta | rc)
    VERSION_ARGS=(--bump "$REQUEST")
    NEW_VERSION="$(
      uv version --package anywidget-mcp --dry-run --short "${VERSION_ARGS[@]}"
    )"
    ;;
  *)
    VERSION_ARGS=("$REQUEST")
    NEW_VERSION="$(
      uv version --package anywidget-mcp --dry-run --short "${VERSION_ARGS[@]}"
    )"
    ;;
esac

TAG="v$NEW_VERSION"
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "Tag already exists: $TAG"

printf '\nRelease summary:\n'
printf '  Current version: %s\n' "$CURRENT_VERSION"
printf '  Release version: %s\n' "$NEW_VERSION"
printf '  Tag:             %s\n' "$TAG"
confirm "Continue?" || die "Release cancelled"

VERSION_UPDATED=0
COMMITTED=0
trap restore_version EXIT

if [[ "$NEW_VERSION" != "$CURRENT_VERSION" ]]; then
  step "Updating package version"
  VERSION_UPDATED=1
  uv version --package anywidget-mcp --no-sync "${VERSION_ARGS[@]}"
  [[ "$(uv version --package anywidget-mcp --short)" == "$NEW_VERSION" ]] \
    || die "Package version did not update to $NEW_VERSION"
fi

step "Validating $TAG"
make check

git diff --quiet -- . \
  ':(exclude)packages/anywidget-mcp/pyproject.toml' \
  ':(exclude)uv.lock' \
  || die "Release checks changed files outside the package version"

git add -- "${VERSION_FILES[@]}"
if ! git diff --cached --quiet; then
  step "Committing $NEW_VERSION"
  if ! git commit -m "release: $NEW_VERSION"; then
    git restore --staged -- "${VERSION_FILES[@]}"
    die "Could not create the release commit"
  fi
  COMMITTED=1
fi

step "Tagging $TAG"
git tag -a "$TAG" -m "release: $NEW_VERSION"
trap - EXIT

cat <<EOF

$TAG is ready locally. Push the commit and tag atomically to publish to PyPI:

  git push --atomic origin main "$TAG"
EOF
