import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
	testDir: "./tests",
	timeout: 30_000,
	expect: { timeout: 10_000 },
	fullyParallel: true,
	forbidOnly: Boolean(process.env.CI),
	retries: 0,
	workers: process.env.CI ? 1 : 2,
	reporter: process.env.CI
		? [["list"], ["github"], ["html", { open: "never" }]]
		: [["list"], ["html", { open: "never" }]],
	use: {
		baseURL: "http://127.0.0.1:4173",
		trace: "retain-on-failure",
		screenshot: "only-on-failure",
	},
	projects: [
		{ name: "chromium", use: { ...devices["Desktop Chrome"] } },
		{
			name: "firefox",
			use: { ...devices["Desktop Firefox"] },
			testIgnore: /webmcp-(jupyter|marimo|native)\.spec\.ts/,
		},
		{
			name: "webkit",
			use: { ...devices["Desktop Safari"] },
			testIgnore: /webmcp-(jupyter|marimo|native)\.spec\.ts/,
		},
	],
	webServer: [
		{
			command: "uv run --locked --package anywidget-mcp-e2e python apps/e2e/jupyter_webmcp.py",
			cwd: "../..",
			url: "http://127.0.0.1:8793/api?token=anywidget-mcp-e2e",
			timeout: 60_000,
			reuseExistingServer: false,
			gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 },
		},
		{
			command:
				"uv run --locked --package anywidget-mcp-e2e marimo run apps/e2e/marimo_webmcp.py --host 127.0.0.1 --port 8792 --no-token --headless --session-ttl 1",
			cwd: "../..",
			url: "http://127.0.0.1:8792/",
			timeout: 60_000,
			reuseExistingServer: false,
			gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 },
		},
		{
			command: "uv run --locked --package anywidget-mcp python apps/e2e/webmcp_server.py",
			cwd: "../..",
			url: "http://127.0.0.1:8767/health",
			timeout: 60_000,
			reuseExistingServer: false,
			gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 },
		},
		{
			command: "uv run --locked --package anywidget-mcp python apps/e2e/server.py",
			cwd: "../..",
			url: "http://127.0.0.1:8766/health",
			timeout: 60_000,
			reuseExistingServer: false,
			gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 },
		},
		{
			command: "vp dev --config vite.config.ts",
			url: "http://127.0.0.1:4173",
			timeout: 30_000,
			reuseExistingServer: false,
		},
	],
});
