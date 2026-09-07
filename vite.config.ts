import { defineConfig } from "vite-plus";

import { antiSlopIgnorePatterns, antiSlopRules } from "./tools/oxlint/anti-slop/preset.ts";

const generated = [
	"apps/e2e/playwright-report/**",
	"apps/e2e/test-results/**",
	"**/*.har",
	"**/.vitepress/cache/**",
	"**/.vitepress/dist/**",
	"dist/**",
	"packages/*/dist/**",
	"packages/anywidget-mcp/src/anywidget_mcp/static/**",
];

const ignored = [...generated, ...antiSlopIgnorePatterns];

export default defineConfig({
	fmt: {
		ignorePatterns: ignored,
		printWidth: 100,
		semi: true,
		useTabs: true,
	},
	lint: {
		categories: {
			correctness: "error",
			perf: "error",
		},
		ignorePatterns: ignored,
		jsPlugins: [
			{ name: "vite-plus", specifier: "vite-plus/oxlint-plugin" },
			{ name: "anti-slop", specifier: "./tools/oxlint/anti-slop/index.ts" },
		],
		options: {
			denyWarnings: true,
			reportUnusedDisableDirectives: "error",
			typeAware: true,
			typeCheck: true,
		},
		plugins: ["typescript", "unicorn", "import"],
		rules: {
			...antiSlopRules,
			"vite-plus/prefer-vite-plus-imports": "error",
		},
		overrides: [
			{
				files: ["packages/app/**"],
				rules: {
					// AnyWidget and DOM callbacks are context-free fields across this package.
					"typescript/unbound-method": "off",
					"no-restricted-imports": [
						"error",
						{
							patterns: ["@anywidget-mcp/python", "@anywidget-mcp/python/*"],
						},
					],
				},
			},
			{
				files: ["packages/app/src/model.ts", "packages/app/src/runtime.ts"],
				rules: {
					// Polling and model operations cross a stateful protocol boundary in order.
					"no-await-in-loop": "off",
				},
			},
		],
	},
	run: {
		cache: true,
	},
});
