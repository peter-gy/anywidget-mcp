import { describe, expect, test } from "vite-plus/test";

import { normalizeMessages } from "../src/runtime-payload";

describe("runtime payload", () => {
	test("rejects malformed message buffer paths", () => {
		expect(() =>
			normalizeMessages([
				{
					data: {
						method: "update",
						state: {},
						buffer_paths: "x",
					},
				},
			]),
		).toThrow("Widget buffer paths must be an array");
	});
});
