#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

usage() {
	cat <<'EOF'
Usage: ./scripts/release.sh [--dry-run]

Releases the package version committed to main. The command requires a clean,
synchronized main branch and a successful CI run for its current commit. It
creates and pushes the annotated v<version> tag that starts trusted publishing.

Add the version change to the release-bearing pull request with:

  uv version --package anywidget-mcp --bump patch
EOF
}

error() {
	printf 'ERROR: %s\n' "$1" >&2
}

require_command() {
	if ! command -v "$1" >/dev/null 2>&1; then
		error "Missing required command: $1"
		exit 1
	fi
}

DRY_RUN=0

case "${1:-}" in
	"") ;;
	--dry-run)
		DRY_RUN=1
		;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		error "Unknown argument: $1"
		usage >&2
		exit 1
		;;
esac

if [[ "$#" -gt 1 ]]; then
	error "Expected at most one argument"
	usage >&2
	exit 1
fi

require_command gh
require_command git
require_command uv

BRANCH="$(git branch --show-current)"
if [[ "$BRANCH" != "main" ]]; then
	error "Releases must run from main. Current branch: $BRANCH"
	exit 1
fi

if [[ -n "$(git status --porcelain)" ]]; then
	error "The working tree must be clean"
	git status --short >&2
	exit 1
fi

git fetch origin main --tags

COMMIT="$(git rev-parse HEAD)"
REMOTE_COMMIT="$(git rev-parse origin/main)"
if [[ "$COMMIT" != "$REMOTE_COMMIT" ]]; then
	error "Local main must match origin/main"
	printf 'Run git pull --ff-only origin main, then retry.\n' >&2
	exit 1
fi

VERSION="$(uv version --package anywidget-mcp --short)"
TAG="v$VERSION"
if git rev-parse -q --verify "refs/tags/$TAG" >/dev/null; then
	error "Release tag already exists: $TAG"
	exit 1
fi

CI_RUN="$(gh run list \
	--workflow ci.yml \
	--branch main \
	--commit "$COMMIT" \
	--event push \
	--limit 1 \
	--json databaseId,status,conclusion,url \
	--jq 'if length == 0 then "" else (.[0] | [.databaseId, .status, .conclusion, .url] | .[]) end')"

if [[ -z "$CI_RUN" ]]; then
	error "No main CI run found for $COMMIT"
	printf 'Wait for the main CI workflow to start, then retry.\n' >&2
	exit 1
fi

{
	IFS= read -r CI_RUN_ID
	IFS= read -r CI_STATUS
	IFS= read -r CI_CONCLUSION
	IFS= read -r CI_URL
} <<<"$CI_RUN"
CI_CONCLUSION="${CI_CONCLUSION:-pending}"
if [[ "$CI_STATUS" != "completed" || "$CI_CONCLUSION" != "success" ]]; then
	error "Main CI must pass before releasing. Current result: $CI_STATUS/$CI_CONCLUSION"
	printf 'CI run: %s\n' "$CI_URL" >&2
	printf 'Run gh run watch %s --exit-status, then retry.\n' "$CI_RUN_ID" >&2
	exit 1
fi

REPOSITORY_URL="$(gh repo view --json url --jq .url)"

printf 'Release: %s\n' "$TAG"
printf 'Commit:  %s\n' "$COMMIT"
printf 'CI:      %s\n' "$CI_URL"

if [[ "$DRY_RUN" == "1" ]]; then
	printf '\nDry run complete. Run ./scripts/release.sh to create and push %s.\n' "$TAG"
	exit 0
fi

git tag -a "$TAG" -m "release: $VERSION"
if ! git push origin "$TAG"; then
	git tag -d "$TAG" >/dev/null
	error "Failed to push $TAG. The local tag was deleted so the command can be retried."
	exit 1
fi

printf '\nRelease %s started.\n' "$TAG"
printf 'Publish workflow: %s/actions/workflows/publish.yml\n' "$REPOSITORY_URL"
