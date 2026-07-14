import { defineConfig } from "vite-plus";

export default defineConfig({
	pack: {
		dts: true,
		entry: ["src/app.ts"],
	},
	test: {
		include: ["tests/**/*.test.ts"],
	},
});
