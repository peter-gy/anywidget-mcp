import { defineConfig, devices } from "@playwright/test";

import shared from "./playwright.config";

export default defineConfig({
	...shared,
	testMatch: "atlas.spec.ts",
	testIgnore: [],
	outputDir: "test-results/atlas",
	reporter: process.env.CI
		? [["list"], ["github"], ["html", { open: "never", outputFolder: "playwright-report/atlas" }]]
		: [["list"], ["html", { open: "never", outputFolder: "playwright-report/atlas" }]],
	timeout: 120_000,
	expect: { timeout: 60_000 },
	fullyParallel: false,
	workers: 1,
	projects: [
		{
			name: "chromium",
			use: {
				...devices["Desktop Chrome"],
				launchOptions: {
					args:
						process.platform === "darwin"
							? ["--enable-unsafe-webgpu", "--use-angle=metal"]
							: [
									"--enable-unsafe-webgpu",
									"--enable-features=Vulkan",
									"--use-angle=vulkan",
									"--use-vulkan=native",
									"--disable-vulkan-surface",
									"--ignore-gpu-blocklist",
								],
				},
			},
		},
	],
	webServer: [
		{
			command: "uv run --locked --project . python atlas_server.py",
			url: "http://127.0.0.1:8766/health",
			timeout: 120_000,
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
