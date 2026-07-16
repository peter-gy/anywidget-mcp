# Releasing

Releases use an annotated version tag as the publishing boundary. The
`Publish` workflow rebuilds and validates the distribution, publishes it to
PyPI through Trusted Publishing, then creates GitHub release notes.

## Trusted publisher

Configure the PyPI project with this publisher identity:

| Field       | Value           |
| ----------- | --------------- |
| Owner       | `peter-gy`      |
| Repository  | `anywidget-mcp` |
| Workflow    | `publish.yml`   |
| Environment | `pypi`          |

The GitHub `pypi` environment is the publishing boundary. The publish job has
`id-token: write` permission and receives the wheel and source distribution
from the validated build job.

## Prepare a release

Start from a clean, current `main` branch. Pass an explicit PEP 440 version to
start a prerelease series:

```sh
./scripts/release.sh 0.0.1rc1
```

The helper updates the package with uv, runs `make check`, creates a
`release: 0.0.1rc1` commit, and adds the annotated `v0.0.1rc1` tag. The tag is
local until you push it.

Advance the candidate series or promote it to the final version:

```sh
./scripts/release.sh rc
./scripts/release.sh stable
```

Use `major`, `minor`, or `patch` for a stable version bump. Run the helper with
no argument when the committed package version is already the intended
release.

Review the commit and tag, then push them atomically:

```sh
git push --atomic origin main v0.0.1rc1
```

The tag must equal `v` followed by the version in
`packages/anywidget-mcp/pyproject.toml`. The workflow stops before publishing
when those values differ.

## Verify the release

Wait for the `Publish` workflow to finish, then inspect the GitHub release and
the PyPI project. Prerelease versions receive the GitHub prerelease flag.

Install from the public index in a fresh environment:

```sh
uv run --isolated --no-project \
  --with 'anywidget-mcp==0.0.1rc1' \
  anywidget-mcp --help
```

PyPI distributions are immutable. Rerun a failed job when the upload completed
but a later release step failed. The release-note job verifies that both public
artifacts exist before creating or updating the GitHub release. Publish a new
version when either accepted artifact differs from the validated build.
