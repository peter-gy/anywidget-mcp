import { describe, expect, test } from "vite-plus/test";

import { normalizeMessages, normalizeModels } from "../src/runtime-payload";

describe("runtime payload", () => {
	test("preserves binary dictionary keys in the initial model graph", () => {
		const [model] = normalizeModels({
			root: {
				state: { _esm: "export default {}", data: {} },
				bufferPaths: [["data", "__proto__"]],
				buffers: [new Uint8Array([1, 2, 3]).buffer],
			},
		});
		const data = model?.state.data;

		expect(Object.getOwnPropertyDescriptor(data, "__proto__")?.value).toEqual(
			new DataView(new Uint8Array([1, 2, 3]).buffer),
		);
		expect(Object.getPrototypeOf(data)).toBe(Object.prototype);
	});

	test("rejects buffer paths outside the model's own state", () => {
		try {
			expect(() =>
				normalizeModels({
					root: {
						state: { _esm: "export default {}" },
						bufferPaths: [["__proto__", "binary"]],
						buffers: [new Uint8Array([1, 2, 3]).buffer],
					},
				}),
			).toThrow("Invalid buffer path");
			expect(Object.hasOwn(Object.prototype, "binary")).toBe(false);
		} finally {
			Reflect.deleteProperty(Object.prototype, "binary");
		}
	});

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
