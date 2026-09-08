.PHONY: check browser package package-artifacts

VP := node_modules/.bin/vp

check:
	$(VP) run check
	$(VP) run -r test
	$(VP) run -r build
	uv lock --check
	uv run ruff format --check packages/anywidget-mcp scripts apps/e2e
	uv run ruff check packages/anywidget-mcp scripts apps/e2e
	uv run ty check packages/anywidget-mcp scripts apps/e2e
	uv run pyrefly check --min-severity warn
	uv run pytest -q packages/anywidget-mcp/tests
	pnpm --filter @anywidget-mcp/e2e e2e
	$(MAKE) package-artifacts
	printf '@AGENTS.md\n' | cmp -s - CLAUDE.md
	git diff --check

browser:
	$(VP) run -F @anywidget-mcp/app build
	$(VP) run -F @anywidget-mcp/python build

package: browser
	$(MAKE) package-artifacts

package-artifacts:
	cmp -s README.md packages/anywidget-mcp/README.md
	cmp -s LICENSE packages/anywidget-mcp/LICENSE
	rm -rf dist
	uv build --package anywidget-mcp --out-dir dist
	uv run --no-project python scripts/verify_package.py \
		"$$(find dist -maxdepth 1 -name '*.whl' -print -quit)" \
		"$$(find dist -maxdepth 1 -name '*.tar.gz' -print -quit)"
	uv run --locked twine check dist/*.whl dist/*.tar.gz
	mkdir -p dist/from-sdist
	uv build --wheel "$$(find dist -maxdepth 1 -name '*.tar.gz' -print -quit)" --out-dir dist/from-sdist
	uv run --no-project --with "$$(find dist/from-sdist -name '*.whl' -print -quit)" python -c 'from importlib.resources import files; import anywidget_mcp; assert files("anywidget_mcp").joinpath("static/index.html").read_bytes().startswith(b"<!doctype html>")'
	uv run --no-project --with "$$(find dist/from-sdist -name '*.whl' -print -quit)[server]" anywidget-mcp --help >/dev/null
