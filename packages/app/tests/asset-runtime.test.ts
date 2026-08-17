import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { afterEach, beforeEach, describe, expect, test, vi } from "vite-plus/test";

import { WidgetRuntime } from "../src/app";
import { clearAssetMemoryCache, widgetAssetId, type AssetKind } from "../src/assets";
import type { RuntimeBinding } from "../src/binding";
import type { BridgeModel } from "../src/model";
import type { RuntimeRecord } from "../src/runtime-value";
import { ToolCallQueue, type ToolRequest } from "../src/tool-calls";
import { fixtureStrings } from "./runtime-test-support";

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

function manifest(...assets: FixtureAsset[]): RuntimeRecord {
	return Object.fromEntries(
		assets.map((asset) => [asset.id, { kind: asset.kind, byteLength: asset.byteLength }]),
	);
}

function assetContents<Value>(
	assets: ReadonlyMap<string, FixtureAsset>,
	assetIds: Value,
): CallToolResult {
	const ids = fixtureStrings(assetIds);
	return {
		content: [],
		_meta: {
			anywidget: {
				protocolVersion: 1,
				assetContents: Object.fromEntries(
					ids.map((assetId) => {
						const asset = assets.get(String(assetId));
						if (!asset) throw new Error(`unknown fixture asset ${String(assetId)}`);
						return [
							asset.id,
							{
								kind: asset.kind,
								byteLength: asset.byteLength,
								text: asset.text,
							},
						];
					}),
				),
			},
		},
	};
}

class RecordingBinding implements RuntimeBinding {
	constructor(
		readonly model: BridgeModel,
		private readonly initialized: RuntimeRecord[],
	) {}

	async initialize(): Promise<void> {
		const [esm, css] = fixtureStrings([this.model.get("_esm"), this.model.get("_css")]);
		this.initialized.push({
			modelId: this.model.modelId,
			esm,
			css,
		});
	}

	async render(): Promise<void> {}

	async getExports(): Promise<RuntimeRecord | undefined> {
		return undefined;
	}

	async dispose(): Promise<void> {}
}

type RuntimePayload = Parameters<typeof WidgetRuntime.create>[0];

function runtimePayload(overrides: Partial<RuntimePayload> = {}): RuntimePayload {
	return {
		protocolVersion: 1,
		instanceId: "invalid-session",
		rootModelId: "root-model",
		assetManifest: {},
		models: {
			"root-model": {
				modelId: "root-model",
				state: {},
			},
		},
		...overrides,
	};
}

const invalidLaunches: Array<[string, RuntimePayload, string]> = [
	[
		"an unversioned payload",
		runtimePayload({ protocolVersion: undefined }),
		"Widget payload protocol version undefined is incompatible with version 1",
	],
	[
		"an incompatible protocol version",
		runtimePayload({ protocolVersion: 2 }),
		"Widget payload protocol version 2 is incompatible with version 1",
	],
	[
		"a missing root model ID",
		runtimePayload({ rootModelId: undefined, models: {} }),
		"Missing root model ID",
	],
	[
		"an inline source",
		runtimePayload({
			models: {
				"root-model": {
					state: { _esm: "export default { render() {} }" },
				},
			},
		}),
		"Widget source _esm must use a content-addressed reference",
	],
	[
		"non-object source references",
		runtimePayload({
			models: { "root-model": { state: {}, sourceRefs: "not-an-object" } },
		}),
		"Widget source references must be an object",
	],
	[
		"an unknown source reference",
		runtimePayload({
			models: {
				"root-model": {
					state: {},
					sourceRefs: { script: `esm:sha256:${"0".repeat(64)}` },
				},
			},
		}),
		"Unknown widget source reference script",
	],
	[
		"an invalid source reference",
		runtimePayload({
			models: {
				"root-model": {
					state: {},
					sourceRefs: { _esm: "esm:sha256:short" },
				},
			},
		}),
		"Invalid widget source reference for _esm",
	],
];

describe("WidgetRuntime asset hydration", () => {
	beforeEach(() => {
		clearAssetMemoryCache();
	});

	afterEach(() => {
		clearAssetMemoryCache();
		vi.unstubAllGlobals();
		vi.restoreAllMocks();
	});

	test("hydrates initial models and launch source updates before exposing the runtime", async () => {
		const initialEsm = await fixtureAsset("esm", "\nexport default { render() {} }\n");
		const initialCss = await fixtureAsset("css", ".initial { color: red; }\n");
		const launchedEsm = await fixtureAsset(
			"esm",
			"\nexport default { render() { /* launch λ */ } }\n",
		);
		const launchedCss = await fixtureAsset(
			"css",
			'[data-state="launch"]::after { content: "✓"; }\n',
		);
		const assets = new Map(
			[initialEsm, initialCss, launchedEsm, launchedCss].map((asset) => [asset.id, asset]),
		);
		const callServerTool = vi.fn(async (request: ToolRequest) => {
			if (request.name === "anywidget_assets") {
				return assetContents(assets, request.arguments?.asset_ids);
			}
			return { content: [] } satisfies CallToolResult;
		});
		const app = {
			callServerTool,
			getHostCapabilities: () => ({}),
			updateModelContext: vi.fn().mockResolvedValue({}),
		};

		const runtime = await WidgetRuntime.create(
			{
				protocolVersion: 1,
				instanceId: "session-1",
				rootModelId: "root-model",
				assetManifest: manifest(initialEsm, initialCss, launchedEsm, launchedCss),
				models: {
					"root-model": {
						modelId: "root-model",
						state: { value: 0 },
						sourceRefs: { _esm: initialEsm.id, _css: initialCss.id },
					},
				},
				messages: [
					{
						modelId: "root-model",
						data: { method: "update", state: { value: 1 } },
						sourceRefs: { _esm: launchedEsm.id, _css: launchedCss.id },
						buffers: [],
					},
				],
			},
			new ToolCallQueue(app),
			app,
			Promise.resolve(),
			(_runtime, model) => new RecordingBinding(model, []),
		);

		const root = runtime.model("root-model");
		expect(root.get("value")).toBe(1);
		expect(root.get("_esm")).toBe(launchedEsm.text);
		expect(root.get("_css")).toBe(launchedCss.text);
		const assetRequest = callServerTool.mock.calls.find(
			([request]) => request.name === "anywidget_assets",
		)?.[0];
		expect(assetRequest).toMatchObject({
			name: "anywidget_assets",
			arguments: { instance_id: "session-1" },
		});
		expect(new Set(fixtureStrings(assetRequest?.arguments?.asset_ids))).toEqual(
			new Set([initialEsm.id, initialCss.id, launchedEsm.id, launchedCss.id]),
		);

		await runtime.dispose();
	});

	test("hydrates dynamic models and hot ESM and CSS inside the active comm transaction", async () => {
		const initialEsm = await fixtureAsset("esm", "export default { render() {} }");
		const initialCss = await fixtureAsset("css", ".initial {}");
		const hotEsm = await fixtureAsset("esm", "\nexport default { render() { /* hot café */ } }\n");
		const hotCss = await fixtureAsset("css", '[data-hot="true"] { content: "λ"; }\n');
		const childCss = await fixtureAsset("css", ".child { color: rgb(1, 2, 3); }\n");
		const assets = new Map(
			[initialEsm, initialCss, hotEsm, hotCss, childCss].map((asset) => [asset.id, asset]),
		);
		const initialized: RuntimeRecord[] = [];
		const names: string[] = [];
		const callServerTool = vi.fn(async (request: ToolRequest) => {
			names.push(request.name);
			if (request.name === "anywidget_assets") {
				return assetContents(assets, request.arguments?.asset_ids);
			}
			if (request.name === "anywidget_comm") {
				return {
					content: [],
					_meta: {
						anywidget: {
							protocolVersion: 1,
							assetManifest: manifest(hotEsm, hotCss, childCss),
							models: {
								"child-model": {
									modelId: "child-model",
									state: { value: 9 },
									sourceRefs: { _esm: hotEsm.id, _css: childCss.id },
								},
							},
							messages: [
								{
									modelId: "root-model",
									data: { method: "update", state: { value: 2 } },
									sourceRefs: { _esm: hotEsm.id, _css: hotCss.id },
									buffers: [],
								},
							],
						},
					},
				} satisfies CallToolResult;
			}
			return { content: [] } satisfies CallToolResult;
		});
		const app = {
			callServerTool,
			getHostCapabilities: () => ({}),
			updateModelContext: vi.fn().mockResolvedValue({}),
		};
		const calls = new ToolCallQueue(app);
		const runtime = await WidgetRuntime.create(
			{
				protocolVersion: 1,
				instanceId: "session-1",
				rootModelId: "root-model",
				assetManifest: manifest(initialEsm, initialCss),
				models: {
					"root-model": {
						modelId: "root-model",
						state: { value: 0 },
						sourceRefs: { _esm: initialEsm.id, _css: initialCss.id },
					},
				},
			},
			calls,
			app,
			Promise.resolve(),
			(_bindingRuntime, model) => new RecordingBinding(model, initialized),
		);

		await runtime.send("root-model", { method: "update", state: { value: 1 } }, [], "operation-1");

		expect(names).toEqual(["anywidget_assets", "anywidget_comm", "anywidget_assets"]);
		expect(runtime.model("root-model").get("value")).toBe(2);
		expect(runtime.model("root-model").get("_esm")).toBe(hotEsm.text);
		expect(runtime.model("root-model").get("_css")).toBe(hotCss.text);
		expect(runtime.model("child-model").get("_esm")).toBe(hotEsm.text);
		expect(runtime.model("child-model").get("_css")).toBe(childCss.text);
		expect(initialized).toEqual([
			{
				modelId: "child-model",
				esm: hotEsm.text,
				css: childCss.text,
			},
		]);
		const dynamicAssetRequest = callServerTool.mock.calls.find(
			([request]) =>
				request.name === "anywidget_assets" &&
				fixtureStrings(request.arguments?.asset_ids).includes(hotEsm.id),
		)?.[0];
		expect(new Set(fixtureStrings(dynamicAssetRequest?.arguments?.asset_ids))).toEqual(
			new Set([hotEsm.id, childCss.id, hotCss.id]),
		);

		await runtime.dispose();
	});

	test.each(invalidLaunches)(
		"rejects %s and disposes its server session",
		async (_case, payload, error) => {
			const app = {
				callServerTool: vi.fn().mockResolvedValue({ content: [] }),
				getHostCapabilities: () => ({}),
				updateModelContext: vi.fn().mockResolvedValue({}),
			};

			await expect(
				WidgetRuntime.create(payload, new ToolCallQueue(app), app, Promise.resolve()),
			).rejects.toThrow(error);
			expect(app.callServerTool).toHaveBeenCalledWith(
				{
					name: "anywidget_dispose",
					arguments: { session_id: "invalid-session" },
				},
				{ signal: expect.any(AbortSignal) },
			);
		},
	);

	test("preserves the creation error when the host ignores disposal cancellation", async () => {
		vi.useFakeTimers();
		try {
			const callServerTool = vi.fn(
				async (): Promise<CallToolResult> => await new Promise<CallToolResult>(() => undefined),
			);
			const app = {
				callServerTool,
				getHostCapabilities: () => ({}),
				updateModelContext: vi.fn().mockResolvedValue({}),
			};
			const creation = WidgetRuntime.create(
				{
					instanceId: "stalled-disposal-session",
					rootModelId: "root-model",
					assetManifest: {},
					models: {},
				},
				new ToolCallQueue(app),
				app,
				Promise.resolve(),
			);
			const creationError = creation.catch((cause: unknown) => cause);

			await vi.waitFor(() => expect(callServerTool).toHaveBeenCalledOnce());
			await vi.advanceTimersByTimeAsync(3000);

			expect(await creationError).toEqual(
				expect.objectContaining({
					message: "Widget payload protocol version undefined is incompatible with version 1",
				}),
			);
		} finally {
			vi.useRealTimers();
		}
	});

	test("aborts stalled source loading and disposes its server session", async () => {
		const esm = await fixtureAsset("esm", "export default { render() {} }");
		const names: string[] = [];
		const callServerTool = vi.fn(async (request: { name: string }): Promise<CallToolResult> => {
			names.push(request.name);
			if (request.name === "anywidget_assets") {
				return await new Promise<CallToolResult>(() => undefined);
			}
			return { content: [] };
		});
		const app = {
			callServerTool,
			getHostCapabilities: () => ({}),
			updateModelContext: vi.fn().mockResolvedValue({}),
		};
		const controller = new AbortController();
		const creation = WidgetRuntime.create(
			{
				protocolVersion: 1,
				instanceId: "cancelled-session",
				rootModelId: "root-model",
				assetManifest: manifest(esm),
				models: {
					"root-model": {
						state: {},
						sourceRefs: { _esm: esm.id },
					},
				},
			},
			new ToolCallQueue(app),
			app,
			Promise.resolve(),
			undefined,
			controller.signal,
		);

		await vi.waitFor(() => expect(names).toEqual(["anywidget_assets"]));
		controller.abort(new DOMException("teardown", "AbortError"));

		await expect(creation).rejects.toMatchObject({ name: "AbortError" });
		expect(names).toEqual(["anywidget_assets", "anywidget_dispose"]);
	});

	test("aborts stalled persistent cache reads and disposes the server session", async () => {
		const esm = await fixtureAsset("esm", "export default { render() {} }");
		const open = vi.fn(async (): Promise<Cache> => await new Promise<Cache>(() => undefined));
		vi.stubGlobal("caches", { open });
		const names: string[] = [];
		const callServerTool = vi.fn(async (request: { name: string }): Promise<CallToolResult> => {
			names.push(request.name);
			return { content: [] };
		});
		const app = {
			callServerTool,
			getHostCapabilities: () => ({}),
			updateModelContext: vi.fn().mockResolvedValue({}),
		};
		const controller = new AbortController();
		const creation = WidgetRuntime.create(
			{
				protocolVersion: 1,
				instanceId: "stalled-cache-session",
				rootModelId: "root-model",
				assetManifest: manifest(esm),
				models: {
					"root-model": {
						state: {},
						sourceRefs: { _esm: esm.id },
					},
				},
			},
			new ToolCallQueue(app),
			app,
			Promise.resolve(),
			undefined,
			controller.signal,
		);

		await vi.waitFor(() => expect(open).toHaveBeenCalledOnce());
		controller.abort(new DOMException("superseded", "AbortError"));

		await expect(creation).rejects.toMatchObject({ name: "AbortError" });
		expect(names).toEqual(["anywidget_dispose"]);
	});

	test("disposes the server session exactly once when asset verification prevents creation", async () => {
		const esm = await fixtureAsset("esm", "export default { render() {} }");
		const names: string[] = [];
		const callServerTool = vi.fn(async (request: ToolRequest) => {
			names.push(request.name);
			if (request.name === "anywidget_assets") {
				return {
					content: [],
					_meta: {
						anywidget: {
							protocolVersion: 1,
							assetContents: {
								[esm.id]: {
									kind: esm.kind,
									byteLength: esm.byteLength,
									text: "export default { render() { throw new Error(); } }",
								},
							},
						},
					},
				} satisfies CallToolResult;
			}
			return { content: [] } satisfies CallToolResult;
		});
		const app = {
			callServerTool,
			getHostCapabilities: () => ({}),
			updateModelContext: vi.fn().mockResolvedValue({}),
		};

		await expect(
			WidgetRuntime.create(
				{
					protocolVersion: 1,
					instanceId: "failed-session",
					rootModelId: "root-model",
					assetManifest: manifest(esm),
					models: {
						"root-model": {
							modelId: "root-model",
							state: {},
							sourceRefs: { _esm: esm.id },
						},
					},
				},
				new ToolCallQueue(app),
				app,
				Promise.resolve(),
			),
		).rejects.toThrow(`Widget asset response failed verification for ${esm.id}`);

		expect(names).toEqual(["anywidget_assets", "anywidget_dispose"]);
		expect(callServerTool.mock.calls.at(-1)?.[0]).toEqual({
			name: "anywidget_dispose",
			arguments: { session_id: "failed-session" },
		});
	});

	test("rejects inline source traits in dynamic messages", async () => {
		const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
		const initialEsm = await fixtureAsset("esm", "export default { render() {} }");
		const assets = new Map([[initialEsm.id, initialEsm]]);
		const callServerTool = vi.fn(async (request: ToolRequest) => {
			if (request.name === "anywidget_assets") {
				return assetContents(assets, request.arguments?.asset_ids);
			}
			if (request.name === "anywidget_comm") {
				return {
					content: [],
					_meta: {
						anywidget: {
							protocolVersion: 1,
							assetManifest: {},
							messages: [
								{
									modelId: "root-model",
									data: { method: "update", state: { _css: ".inline {}" } },
									buffers: [],
								},
							],
						},
					},
				} satisfies CallToolResult;
			}
			return { content: [] } satisfies CallToolResult;
		});
		const app = {
			callServerTool,
			getHostCapabilities: () => ({}),
			updateModelContext: vi.fn().mockResolvedValue({}),
		};
		const runtime = await WidgetRuntime.create(
			{
				protocolVersion: 1,
				instanceId: "dynamic-inline-session",
				rootModelId: "root-model",
				assetManifest: manifest(initialEsm),
				models: {
					"root-model": {
						state: {},
						sourceRefs: { _esm: initialEsm.id },
					},
				},
			},
			new ToolCallQueue(app),
			app,
			Promise.resolve(),
			(_runtime, model) => new RecordingBinding(model, []),
		);

		await expect(
			runtime.send("root-model", { method: "update", state: {} }, [], "inline-operation"),
		).rejects.toThrow("Widget source _css must use a content-addressed reference");
		await vi.waitFor(() =>
			expect(
				callServerTool.mock.calls.filter(([request]) => request.name === "anywidget_dispose"),
			).toHaveLength(1),
		);
		expect(runtime.model("root-model").get("_css")).toBeUndefined();
		expect(consoleError).toHaveBeenCalledWith(
			expect.objectContaining({
				message: "Widget source _css must use a content-addressed reference",
			}),
		);
	});
});
