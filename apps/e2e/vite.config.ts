import { defineConfig } from "vite-plus";

export default defineConfig({
	root: "host",
	server: {
		host: "127.0.0.1",
		port: 4173,
		strictPort: true,
		proxy: { "/mcp": "http://127.0.0.1:8766" },
	},
});
