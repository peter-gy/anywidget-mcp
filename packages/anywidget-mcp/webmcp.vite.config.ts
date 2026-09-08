import { fileURLToPath } from "node:url";

import { defineConfig } from "vite-plus";

export default defineConfig({
	build: {
		target: "es2022",
		outDir: fileURLToPath(new URL("src/anywidget_mcp/static", import.meta.url)),
		emptyOutDir: false,
		minify: false,
		lib: {
			entry: fileURLToPath(new URL("frontend/webmcp.ts", import.meta.url)),
			formats: ["es"],
			fileName: "webmcp",
		},
	},
});
