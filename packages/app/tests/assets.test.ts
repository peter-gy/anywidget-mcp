import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { afterEach, beforeEach, describe, expect, test, vi } from "vite-plus/test";

import {
	AssetMemoryCache,
	AssetStore,
	clearAssetMemoryCache,
	hydrateSources,
	normalizeSourceRefs,
	requireProtocolVersion,
	widgetAssetId,
	type AssetKind,
} from "../src/assets";
import type { QueuedToolCall } from "../src/tool-calls";

interface FixtureAsset {
	id: string;
	kind: AssetKind;
	text: string;
	byteLength: number;
}

async function fixtureAsset(kind: AssetKind, text: string): Promise<FixtureAsset> {
	return {
		id: await widgetAssetId(kind, text),
		kind,
		text,
		byteLength: new TextEncoder().encode(text).byteLength,
	};
}

function manifest(...assets: FixtureAsset[]): Record<string, unknown> {
	return Object.fromEntries(
		assets.map((asset) => [asset.id, { kind: asset.kind, byteLength: asset.byteLength }]),
	);
}

function assetResult(...assets: FixtureAsset[]): CallToolResult {
	return {
		content: [],
		_meta: {
			anywidget: {
				protocolVersion: 1,
				assetContents: Object.fromEntries(
					assets.map((asset) => [
						asset.id,
						{
							kind: asset.kind,
							byteLength: asset.byteLength,
							text: asset.text,
						},
					]),
				),
			},
		},
	};
}

function cacheKey(assetId: string): string {
	return `https://anywidget-mcp.invalid/assets/${encodeURIComponent(assetId)}`;
}

function installCache(initial: Map<string, string> = new Map()): {
	cache: Pick<Cache, "match" | "put" | "delete">;
	stored: Map<string, Response>;
} {
	const stored = new Map<string, Response>(
		Array.from(initial, ([key, value]) => [key, new Response(value)]),
	);
	const key = (request: RequestInfo | URL): string => {
		if (typeof request === "string") return request;
		if (request instanceof URL) return request.href;
		return request.url;
	};
	const cache = {
		match: vi.fn(async (request: RequestInfo | URL) => stored.get(key(request))?.clone()),
		put: vi.fn(async (request: RequestInfo | URL, response: Response) => {
			stored.set(key(request), response.clone());
		}),
		delete: vi.fn(async (request: RequestInfo | URL) => stored.delete(key(request))),
	};
	vi.stubGlobal("caches", {
		open: vi.fn(async () => cache),
	});
	return { cache, stored };
}

describe("content-addressed widget assets", () => {
	beforeEach(() => {
		clearAssetMemoryCache();
	});

	afterEach(() => {
		clearAssetMemoryCache();
		vi.unstubAllGlobals();
		vi.restoreAllMocks();
	});

	test("derives deterministic kind-scoped IDs from exact UTF-8 source", async () => {
		await expect(widgetAssetId("esm", "export default {}")).resolves.toBe(
			"esm:sha256:c27f60920940fbed65b9dc65b34a4ae43e526eb878522771ec56c3a54d7196bf",
		);
		await expect(widgetAssetId("css", "export default {}")).resolves.toMatch(
			/^css:sha256:[0-9a-f]{64}$/,
		);
		await expect(widgetAssetId("css", "export default {}")).resolves.not.toBe(
			await widgetAssetId("esm", "export default {}"),
		);
		await expect(widgetAssetId("esm", "café")).resolves.not.toBe(
			await widgetAssetId("esm", "cafe\u0301"),
		);
	});

	test("bounds memory entries by UTF-8 byte length and evicts the least recent", () => {
		const cache = new AssetMemoryCache(5);
		cache.set("first", "aaa", 3);
		cache.set("second", "bb", 2);
		expect(cache.get("first")).toBe("aaa");

		cache.set("third", "cc", 2);
		expect(cache.get("second")).toBeUndefined();
		expect(cache.get("first")).toBe("aaa");
		expect(cache.get("third")).toBe("cc");

		cache.set("oversized", "123456", 6);
		expect(cache.get("oversized")).toBeUndefined();
	});

	test("deduplicates references and fetches all cache misses in one batch", async () => {
		const esm = await fixtureAsset("esm", "export default { render() {} }");
		const css = await fixtureAsset("css", ".widget { color: rebeccapurple; }");
		const call = vi.fn<QueuedToolCall>(async () => assetResult(esm, css));

		const resolved = await new AssetStore("session-1").resolve(
			manifest(esm, css),
			[{ _esm: esm.id, _css: css.id }, { _esm: esm.id }, { _esm: esm.id, _css: css.id }],
			call,
		);

		expect(call).toHaveBeenCalledTimes(1);
		expect(call).toHaveBeenCalledWith("anywidget_assets", {
			instance_id: "session-1",
			asset_ids: [esm.id, css.id],
		});
		expect(resolved).toEqual(
			new Map([
				[esm.id, esm.text],
				[css.id, css.text],
			]),
		);
	});

	test("releases an asset fetch when its caller ignores cancellation", async () => {
		const esm = await fixtureAsset("esm", "export default {}");
		const call = vi.fn<QueuedToolCall>(
			async () => await new Promise<CallToolResult>(() => undefined),
		);
		const controller = new AbortController();
		const result = new AssetStore("session").resolve(
			manifest(esm),
			[{ _esm: esm.id }],
			call,
			controller.signal,
		);
		await vi.waitFor(() => expect(call).toHaveBeenCalledOnce());

		controller.abort(new DOMException("initializer stopped", "AbortError"));

		await expect(result).rejects.toMatchObject({ name: "AbortError" });
	});

	test("chunks more than 128 cache misses into bounded fetch requests", async () => {
		const assets = await Promise.all(
			Array.from({ length: 129 }, (_, index) =>
				fixtureAsset("esm", `export default { render() { return ${index}; } }`),
			),
		);
		const byId = new Map(assets.map((asset) => [asset.id, asset]));
		let activeCalls = 0;
		let maxActiveCalls = 0;
		const call = vi.fn<QueuedToolCall>(async (_name, args) => {
			activeCalls += 1;
			maxActiveCalls = Math.max(maxActiveCalls, activeCalls);
			await Promise.resolve();
			try {
				const assetIds = args.asset_ids;
				if (!Array.isArray(assetIds)) throw new Error("missing asset IDs");
				return assetResult(
					...assetIds.map((assetId) => {
						const asset = byId.get(String(assetId));
						if (!asset) throw new Error(`unknown fixture asset ${String(assetId)}`);
						return asset;
					}),
				);
			} finally {
				activeCalls -= 1;
			}
		});

		const resolved = await new AssetStore("session-1").resolve(
			manifest(...assets),
			assets.map((asset) => ({ _esm: asset.id })),
			call,
		);

		expect(call).toHaveBeenCalledTimes(2);
		const batches = call.mock.calls.map(([_name, args]) => args.asset_ids as string[]);
		expect(batches.map((batch) => batch.length)).toEqual([128, 1]);
		expect(maxActiveCalls).toBe(1);
		expect(batches.every((batch) => batch.length <= 128)).toBe(true);
		expect(new Set(batches.flat())).toEqual(new Set(assets.map((asset) => asset.id)));
		expect(resolved.size).toBe(129);
		for (const asset of assets) expect(resolved.get(asset.id)).toBe(asset.text);
	});

	test("reuses verified memory entries across widget sessions", async () => {
		const esm = await fixtureAsset("esm", "export default { render() {} }");
		const firstCall = vi.fn<QueuedToolCall>(async () => assetResult(esm));
		const secondCall = vi.fn<QueuedToolCall>(async () => {
			throw new Error("memory hit should not call the server");
		});

		await new AssetStore("session-1").resolve(manifest(esm), [{ _esm: esm.id }], firstCall);
		const resolved = await new AssetStore("session-2").resolve(
			manifest(esm),
			[{ _esm: esm.id }],
			secondCall,
		);

		expect(firstCall).toHaveBeenCalledTimes(1);
		expect(secondCall).not.toHaveBeenCalled();
		expect(resolved.get(esm.id)).toBe(esm.text);
	});

	test("persists verified assets and reuses them after the memory cache is cleared", async () => {
		const esm = await fixtureAsset("esm", "export default { render() {} }");
		const { cache } = installCache();
		const firstCall = vi.fn<QueuedToolCall>(async () => assetResult(esm));

		await new AssetStore("session-1").resolve(manifest(esm), [{ _esm: esm.id }], firstCall);
		await vi.waitFor(() => expect(cache.put).toHaveBeenCalledTimes(1));
		clearAssetMemoryCache();

		const secondCall = vi.fn<QueuedToolCall>(async () => {
			throw new Error("persistent hit should not call the server");
		});
		const resolved = await new AssetStore("session-2").resolve(
			manifest(esm),
			[{ _esm: esm.id }],
			secondCall,
		);

		expect(secondCall).not.toHaveBeenCalled();
		expect(cache.match).toHaveBeenCalledWith(cacheKey(esm.id));
		expect(resolved.get(esm.id)).toBe(esm.text);
	});

	test("deletes corrupt persistent content and falls back to the session fetch", async () => {
		const esm = await fixtureAsset("esm", "export default { render() {} }");
		const { cache } = installCache(new Map([[cacheKey(esm.id), "corrupt source"]]));
		const call = vi.fn<QueuedToolCall>(async () => assetResult(esm));

		const resolved = await new AssetStore("session-1").resolve(
			manifest(esm),
			[{ _esm: esm.id }],
			call,
		);

		expect(cache.delete).toHaveBeenCalledWith(cacheKey(esm.id));
		expect(call).toHaveBeenCalledTimes(1);
		expect(resolved.get(esm.id)).toBe(esm.text);
	});

	test("falls back to the session fetch when Cache Storage is unavailable", async () => {
		const esm = await fixtureAsset("esm", "export default { render() {} }");
		vi.stubGlobal("caches", {
			open: vi.fn(async () => Promise.reject(new Error("opaque origin"))),
		});
		const call = vi.fn<QueuedToolCall>(async () => assetResult(esm));

		const resolved = await new AssetStore("session-1").resolve(
			manifest(esm),
			[{ _esm: esm.id }],
			call,
		);

		expect(call).toHaveBeenCalledTimes(1);
		expect(resolved.get(esm.id)).toBe(esm.text);
	});

	test("hydrates ESM and CSS with their exact source text", async () => {
		const esm = await fixtureAsset("esm", "\nexport default { render() { /* café λ */ } }\n");
		const css = await fixtureAsset("css", '[data-label="λ"]::after { content: "✓"; }\n');

		expect(
			hydrateSources(
				{ value: 7 },
				{ _esm: esm.id, _css: css.id },
				new Map([
					[esm.id, esm.text],
					[css.id, css.text],
				]),
			),
		).toEqual({ value: 7, _esm: esm.text, _css: css.text });
	});

	test("rejects inline source traits in a versioned wire payload", () => {
		expect(() => hydrateSources({ _esm: "inline" }, {}, new Map())).toThrow(
			"Widget source _esm must use a content-addressed reference",
		);
		expect(() => hydrateSources({ _css: "inline" }, {}, new Map())).toThrow(
			"Widget source _css must use a content-addressed reference",
		);
	});

	test("rejects incompatible protocol versions", () => {
		expect(() => requireProtocolVersion(undefined)).toThrow(
			"Widget payload protocol version undefined is incompatible with version 1",
		);
		expect(() => requireProtocolVersion(2)).toThrow(
			"Widget payload protocol version 2 is incompatible with version 1",
		);
	});

	test("rejects malformed source references", async () => {
		const esm = await fixtureAsset("esm", "export default {}");

		expect(() => normalizeSourceRefs("not-an-object")).toThrow(
			"Widget source references must be an object",
		);
		expect(() => normalizeSourceRefs({ script: esm.id })).toThrow(
			"Unknown widget source reference script",
		);
		expect(() => normalizeSourceRefs({ _esm: "esm:sha256:short" })).toThrow(
			"Invalid widget source reference for _esm",
		);
	});

	test("rejects malformed, missing, unreferenced, and kind-mismatched manifests", async () => {
		const esm = await fixtureAsset("esm", "export default {}");
		const css = await fixtureAsset("css", ".widget {}");
		const unusedCall = vi.fn<QueuedToolCall>();

		await expect(
			new AssetStore("session").resolve(
				{ "esm:sha256:short": { kind: "esm", byteLength: 1 } },
				[],
				unusedCall,
			),
		).rejects.toThrow("Invalid widget asset manifest entry esm:sha256:short");
		await expect(
			new AssetStore("session").resolve(
				{ [esm.id]: { kind: "css", byteLength: esm.byteLength } },
				[{ _esm: esm.id }],
				unusedCall,
			),
		).rejects.toThrow(`Invalid widget asset manifest entry ${esm.id}`);
		await expect(
			new AssetStore("session").resolve({}, [{ _esm: esm.id }], unusedCall),
		).rejects.toThrow(`Missing manifest entry for widget asset ${esm.id}`);
		await expect(new AssetStore("session").resolve(manifest(esm), [], unusedCall)).rejects.toThrow(
			`Widget asset manifest contains unreferenced asset ${esm.id}`,
		);
		await expect(
			new AssetStore("session").resolve(manifest(css), [{ _esm: css.id }], unusedCall),
		).rejects.toThrow(`Widget asset ${css.id} has the wrong source kind for _esm`);
		expect(unusedCall).not.toHaveBeenCalled();
	});

	test("rejects asset responses with missing, unexpected, or malformed contents", async () => {
		const esm = await fixtureAsset("esm", "export default {}");
		const extra = await fixtureAsset("esm", "export default { render() {} }");
		const store = new AssetStore("session");

		await expect(
			store.resolve(manifest(esm), [{ _esm: esm.id }], async () => ({
				content: [],
				_meta: { anywidget: { protocolVersion: 2, assetContents: {} } },
			})),
		).rejects.toThrow("Widget asset response uses an incompatible protocol version");
		await expect(
			store.resolve(manifest(esm), [{ _esm: esm.id }], async () => ({
				content: [],
				_meta: { anywidget: { protocolVersion: 1, assetContents: {} } },
			})),
		).rejects.toThrow(`Widget asset response is missing ${esm.id}`);
		await expect(
			store.resolve(manifest(esm), [{ _esm: esm.id }], async () => assetResult(esm, extra)),
		).rejects.toThrow(`Widget asset response contains unexpected asset ${extra.id}`);
		await expect(
			store.resolve(manifest(esm), [{ _esm: esm.id }], async () => ({
				content: [],
				_meta: {
					anywidget: {
						protocolVersion: 1,
						assetContents: {
							[esm.id]: { kind: "esm", byteLength: esm.byteLength, text: 7 },
						},
					},
				},
			})),
		).rejects.toThrow(`Invalid widget asset content ${esm.id}`);
	});

	test.each([
		["kind", { kind: "css" }],
		["byte length", { byteLength: 1 }],
		["digest", { text: "export default []" }],
	])("rejects fetched content with a mismatched %s", async (_label, override) => {
		const esm = await fixtureAsset("esm", "export default {}");
		const content = {
			kind: esm.kind,
			byteLength: esm.byteLength,
			text: esm.text,
			...override,
		};

		await expect(
			new AssetStore("session").resolve(manifest(esm), [{ _esm: esm.id }], async () => ({
				content: [],
				_meta: {
					anywidget: {
						protocolVersion: 1,
						assetContents: { [esm.id]: content },
					},
				},
			})),
		).rejects.toThrow(`Widget asset response failed verification for ${esm.id}`);
	});
});
