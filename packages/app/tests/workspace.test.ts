import { describe, expect, test } from "vite-plus/test";

import manifest from "../package.json";

const dependencyFields = [
	"dependencies",
	"devDependencies",
	"peerDependencies",
	"optionalDependencies",
] as const;
const dependencies: Partial<Record<(typeof dependencyFields)[number], Record<string, string>>> =
	manifest;

describe("workspace boundary", () => {
	test("keeps the Python composition package out of the app manifest", () => {
		for (const field of dependencyFields) {
			expect(dependencies[field] ?? {}).not.toHaveProperty("@anywidget-mcp/python");
		}
	});
});
