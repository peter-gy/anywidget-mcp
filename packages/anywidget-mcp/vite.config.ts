import { fileURLToPath } from "node:url";

import { defineConfig } from "vite-plus";
import { viteSingleFile } from "vite-plugin-singlefile";

export default defineConfig({
	root: "frontend",
	plugins: [viteSingleFile()],
	build: {
		target: "es2022",
		outDir: fileURLToPath(new URL("src/anywidget_mcp/static", import.meta.url)),
		emptyOutDir: true,
		cssMinify: true,
		minify: true,
	},
});
