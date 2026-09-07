import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
	testDir: "./tests",
	testIgnore: "atlas.spec.ts",
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
		{ name: "firefox", use: { ...devices["Desktop Firefox"] } },
		{ name: "webkit", use: { ...devices["Desktop Safari"] } },
	],
	webServer: [
		{
			command: "uv run --locked --package anywidget-mcp python apps/e2e/server.py",
			cwd: "../..",
			url: "http://127.0.0.1:8766/health",
			timeout: 60_000,
			reuseExistingServer: false,
			gracefulShutdown: { signal: "SIGTERM", timeout: 5_000 },
		},
		{
			command: "pnpm exec vp dev --config vite.config.ts",
			url: "http://127.0.0.1:4173",
			timeout: 30_000,
			reuseExistingServer: false,
		},
	],
});
