import { defineConfig } from "vite-plus";

const generated = [
	"**/*.har",
	"dist/**",
	"packages/*/dist/**",
	"packages/anywidget-mcp/src/anywidget_mcp/static/**",
];

export default defineConfig({
	fmt: {
		ignorePatterns: generated,
		printWidth: 100,
		semi: true,
		useTabs: true,
	},
	lint: {
		categories: {
			correctness: "error",
			perf: "error",
		},
		ignorePatterns: generated,
		jsPlugins: [{ name: "vite-plus", specifier: "vite-plus/oxlint-plugin" }],
		options: {
			typeAware: true,
			typeCheck: true,
		},
		plugins: ["typescript", "unicorn", "import"],
		rules: {
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
				files: ["packages/app/src/app.ts", "packages/app/src/model.ts"],
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
