.PHONY: check package

check:
	pnpm check
	pnpm test
	pnpm build
	uv lock --check
	uv run --package anywidget-mcp ruff format --check packages/anywidget-mcp scripts
	uv run --package anywidget-mcp ruff check packages/anywidget-mcp scripts
	uv run --package anywidget-mcp ty check packages/anywidget-mcp scripts
	uv run --package anywidget-mcp pyrefly check --min-severity warn
	uv run --package anywidget-mcp pytest -q packages/anywidget-mcp/tests
	$(MAKE) package
	printf '@AGENTS.md\n' | cmp -s - CLAUDE.md
	git diff --check

package:
	cmp -s README.md packages/anywidget-mcp/README.md
	cmp -s LICENSE packages/anywidget-mcp/LICENSE
	rm -rf dist
	pnpm build
	uv build --package anywidget-mcp --out-dir dist
	uv run --no-project python scripts/verify_package.py \
		"$$(find dist -maxdepth 1 -name '*.whl' -print -quit)" \
		"$$(find dist -maxdepth 1 -name '*.tar.gz' -print -quit)"
	uvx twine check dist/*.whl dist/*.tar.gz
	mkdir -p dist/from-sdist
	uv build --wheel "$$(find dist -maxdepth 1 -name '*.tar.gz' -print -quit)" --out-dir dist/from-sdist
	uv run --no-project --with "$$(find dist/from-sdist -name '*.whl' -print -quit)" python -c 'from importlib.resources import files; import anywidget_mcp; assert files("anywidget_mcp").joinpath("static/index.html").read_bytes().startswith(b"<!doctype html>")'
	uv run --no-project --with "$$(find dist/from-sdist -name '*.whl' -print -quit)" anywidget-mcp --help >/dev/null
