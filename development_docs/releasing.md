# Releasing

A release-bearing pull request updates the package version. CI validates that
commit and builds the distributions. After the pull request is merged,
`scripts/release.sh` tags the green `main` commit. The publish workflow selects
that commit's successful CI run and publishes its validated distributions.

## Trusted publisher

Configure the PyPI project with this publisher identity:

| Field       | Value           |
| ----------- | --------------- |
| Owner       | `peter-gy`      |
| Repository  | `anywidget-mcp` |
| Workflow    | `publish.yml`   |
| Environment | `pypi`          |

The `pypi` environment grants the publish job an OpenID Connect token through
[PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/). The
workflow publishes the wheel and source distribution from CI, installs the
public package, and creates the GitHub release notes.

## Prepare the version

Create a branch and update the package version with uv:

```sh
git switch -c pgy/release-0.0.3
uv version --package anywidget-mcp 0.0.3
git add packages/anywidget-mcp/pyproject.toml uv.lock
git commit -m "chore: release 0.0.3"
```

Open a pull request and merge it after the `required` CI check passes.

## Start the release

Update local `main`, inspect the release boundary, then push the tag:

```sh
git switch main
git pull --ff-only origin main
./scripts/release.sh --dry-run
./scripts/release.sh
```

The helper requires a clean `main` branch that matches `origin/main` and has a
successful push CI run. It creates and pushes the annotated `v<version>` tag.
The package version remains the PEP 440 value without the `v` prefix.

The publish workflow also verifies the annotated tag, package version, and
successful CI run for that commit. CI retains the `dist` artifact for 30 days.
If it has expired, rerun CI for the release commit before retrying publication.

## Verify the release

Wait for the `Publish package` workflow to finish. It verifies both artifacts
on the public PyPI index and installs the released package in an isolated uv
environment.

You can repeat the public installation check with:

```sh
uv run --no-cache --no-project --isolated \
  --default-index https://pypi.org/simple \
  --with 'anywidget-mcp==0.0.3' \
  python scripts/verify_release.py 0.0.3
```

PyPI distributions are immutable. Publish a new version when either accepted
artifact differs from the validated build.
