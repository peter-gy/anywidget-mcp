import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { defineConfig } from "vite-plus";
import { viteSingleFile } from "vite-plugin-singlefile";

import { thirdPartyNotices } from "./license-notices";

const pyproject = readFileSync(new URL("pyproject.toml", import.meta.url), "utf8");
const version = /^version = "([^"]+)"$/m.exec(pyproject)?.[1];
if (!version) throw new Error("Could not read the anywidget-mcp package version");

export default defineConfig({
	root: "frontend",
	plugins: [
		viteSingleFile(),
		thirdPartyNotices(fileURLToPath(new URL("THIRD_PARTY_NOTICES", import.meta.url))),
	],
	define: {
		__ANYWIDGET_MCP_VERSION__: JSON.stringify(version),
	},
	build: {
		target: "es2022",
		outDir: fileURLToPath(new URL("src/anywidget_mcp/static", import.meta.url)),
		emptyOutDir: true,
		cssMinify: true,
		minify: true,
	},
});
