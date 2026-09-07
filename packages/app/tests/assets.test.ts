import { afterEach, beforeEach, describe, expect, test, vi } from "vite-plus/test";

import { hydrateSources, resolveSources } from "../src/assets";
import { AttachmentStore } from "../src/attachments";
import { fixtureBlob, readResult } from "./attachment-test-support";

beforeEach(() => {
	let pending = Promise.resolve();
	vi.stubGlobal("navigator", {
		locks: {
			request: (_name: string, _options: LockOptions, callback: () => Promise<void>) => {
				const result = pending.then(callback);
				pending = result.catch(() => undefined);
				return result;
			},
		},
	});
});

function installCache() {
	const stored = new Map<string, Response>();
	const cache = {
		match: vi.fn(async (key: string | Request) =>
			stored.get(key instanceof Request ? key.url : key)?.clone(),
		),
		keys: vi.fn(async () => Array.from(stored.keys(), (key) => new Request(key))),
		put: vi.fn(async (key: string, value: Response) => {
			stored.set(key, value.clone());
		}),
		delete: vi.fn(async (key: string | Request) =>
			stored.delete(key instanceof Request ? key.url : key),
		),
	};
	vi.stubGlobal("caches", { open: vi.fn(async () => cache) });
	return { stored, cache };
}

afterEach(() => vi.unstubAllGlobals());

describe("content-addressed sources", () => {
	test("decodes multibyte UTF-8 after chunk assembly and reuses identical ESM and CSS bytes", async () => {
		const source = " ".repeat(65535) + "λ✓ café";
		const blob = await fixtureBlob(source);
		const call = vi.fn(async (_name, args) => readResult(blob.bytes, args));
		const sources = await resolveSources(
			[{ _esm: blob.ref, _css: blob.ref }],
			new AttachmentStore("s"),
			call,
		);
		expect(hydrateSources({}, { _esm: blob.ref, _css: blob.ref }, sources)).toEqual({
			_esm: source,
			_css: source,
		});
		expect(call.mock.calls.map(([, args]) => args.offset)).toEqual([0, 65536]);
	});

	test("persists verified source bytes across sessions while widget data stays session-local", async () => {
		const { cache } = installCache();
		const source = await fixtureBlob("export default {};");
		const data = await fixtureBlob(new Uint8Array([1, 2, 3]));
		const call = vi.fn(async (_name, args) =>
			readResult(args.blob_id === source.ref.id ? source.bytes : data.bytes, args),
		);
		const first = new AttachmentStore("first");
		await resolveSources([{ _esm: source.ref }], first, call);
		await first.read(data.ref, call);
		await vi.waitFor(() => expect(cache.put).toHaveBeenCalledOnce());
		const second = new AttachmentStore("second");
		await resolveSources([{ _esm: source.ref }], second, call);
		await second.read(data.ref, call);
		expect(call.mock.calls.map(([, args]) => args.instance_id)).toEqual([
			"first",
			"first",
			"second",
		]);
	});

	test("verifies cached source bytes before rendering", async () => {
		const { cache, stored } = installCache();
		const source = await fixtureBlob("export default {};");
		stored.set(
			`https://anywidget-mcp.invalid/sources/${encodeURIComponent(source.ref.id)}`,
			new Response("x".repeat(source.bytes.length)),
		);
		const call = vi.fn(async (_name, args) => readResult(source.bytes, args));
		const resolved = await resolveSources([{ _esm: source.ref }], new AttachmentStore("s"), call);
		expect(resolved.get(source.ref.id)).toBe("export default {};");
		expect(cache.delete).toHaveBeenCalledWith(
			`https://anywidget-mcp.invalid/sources/${encodeURIComponent(source.ref.id)}`,
		);
		expect(call).toHaveBeenCalledOnce();
	});

	test("continues through unavailable persistent storage", async () => {
		vi.stubGlobal("caches", { open: vi.fn().mockRejectedValue(new Error("denied")) });
		const source = await fixtureBlob("source");
		const resolved = await resolveSources(
			[{ _esm: source.ref }],
			new AttachmentStore("s"),
			async (_name, args) => readResult(source.bytes, args),
		);
		expect(resolved.get(source.ref.id)).toBe("source");
	});

	test.each([
		[{ _esm: "inline" }, "Invalid widget attachment reference"],
		[{ script: {} }, "Unknown widget source reference script"],
		["bad", "Widget source references must be an object"],
	])("rejects malformed source references %j", async (refs, message) => {
		await expect(resolveSources([refs], new AttachmentStore("s"), vi.fn())).rejects.toThrow(
			String(message),
		);
	});

	test("requires content-addressed source traits", () => {
		expect(() => hydrateSources({ _esm: "inline" }, {}, new Map())).toThrow(
			"Widget source _esm must use a content-addressed reference",
		);
	});
});

test("evicts its oldest source after browser quota rejection and retries storage", async () => {
	const { stored, cache } = installCache();
	const foreign = "https://another-app.invalid/entry";
	const oldKey = "https://anywidget-mcp.invalid/sources/old";
	const recentKey = "https://anywidget-mcp.invalid/sources/recent";
	stored.set(foreign, new Response("unrelated"));
	stored.set(oldKey, new Response("old"));
	stored.set(recentKey, new Response("recent"));
	cache.put.mockRejectedValueOnce(new DOMException("Storage quota reached", "QuotaExceededError"));
	const source = await fixtureBlob("export default {};");
	await resolveSources([{ _esm: source.ref }], new AttachmentStore("s"), async (_name, args) =>
		readResult(source.bytes, args),
	);
	await vi.waitFor(() => expect(cache.put).toHaveBeenCalledTimes(2));
	expect(stored.has(oldKey)).toBe(false);
	expect(stored.has(recentKey)).toBe(true);
	expect(stored.has(foreign)).toBe(true);
	const response = stored.get(
		`https://anywidget-mcp.invalid/sources/${encodeURIComponent(source.ref.id)}`,
	);
	expect(await response?.text()).toBe("export default {};");
});

test("loads sources using session attachments when cross-document cache locking is unavailable", async () => {
	vi.stubGlobal("navigator", {});
	const open = vi.fn();
	vi.stubGlobal("caches", { open });
	const source = await fixtureBlob("export default {};");
	const call = vi.fn(async (_name, args) => readResult(source.bytes, args));
	const sources = await resolveSources([{ _esm: source.ref }], new AttachmentStore("s"), call);
	expect(sources.get(source.ref.id)).toBe("export default {};");
	expect(open).not.toHaveBeenCalled();
});

test("loads a source when browser storage remains full after its cache is emptied", async () => {
	const { cache, stored } = installCache();
	const foreign = "https://another-app.invalid/entry";
	stored.set(foreign, new Response("unrelated"));
	stored.set("https://anywidget-mcp.invalid/sources/old", new Response("old"));
	cache.put.mockRejectedValue(new DOMException("Storage quota reached", "QuotaExceededError"));
	const source = await fixtureBlob("export default {};");
	const sources = await resolveSources(
		[{ _esm: source.ref }],
		new AttachmentStore("s"),
		async (_name, args) => readResult(source.bytes, args),
	);
	await vi.waitFor(() => expect(cache.put).toHaveBeenCalledTimes(2));
	expect(sources.get(source.ref.id)).toBe("export default {};");
	expect(Array.from(stored.keys())).toEqual([foreign]);
});
