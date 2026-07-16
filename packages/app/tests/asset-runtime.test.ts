import type { App } from "@modelcontextprotocol/ext-apps";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { afterEach, beforeEach, describe, expect, test, vi } from "vite-plus/test";

import { WidgetRuntime } from "../src/app";
import { clearAssetMemoryCache, widgetAssetId, type AssetKind } from "../src/assets";
import type { RuntimeBinding } from "../src/binding";
import type { BridgeModel } from "../src/model";
import { ToolCallQueue } from "../src/tool-calls";

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

function assetContents(
	assets: ReadonlyMap<string, FixtureAsset>,
	assetIds: unknown,
): CallToolResult {
	if (!Array.isArray(assetIds)) throw new Error("missing asset IDs");
	return {
		content: [],
		_meta: {
			anywidget: {
				protocolVersion: 1,
				assetContents: Object.fromEntries(
					assetIds.map((assetId) => {
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
		private readonly initialized: Array<Record<string, unknown>>,
	) {}

	async initialize(): Promise<void> {
		this.initialized.push({
			modelId: this.model.modelId,
			esm: this.model.get("_esm"),
			css: this.model.get("_css"),
		});
	}

	async render(): Promise<void> {}

	async getExports(): Promise<unknown> {
		return undefined;
	}

	async dispose(): Promise<void> {}
}

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
		const callServerTool = vi.fn(
			async (request: { name: string; arguments?: Record<string, unknown> }) => {
				if (request.name === "anywidget_assets") {
					return assetContents(assets, request.arguments?.asset_ids);
				}
				return { content: [] } satisfies CallToolResult;
			},
		);
		const app = {
			callServerTool,
			getHostCapabilities: () => ({}),
			updateModelContext: vi.fn().mockResolvedValue({}),
		} as unknown as App;

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
		expect(callServerTool).toHaveBeenCalledTimes(1);
		expect(callServerTool).toHaveBeenCalledWith(
			{
				name: "anywidget_assets",
				arguments: {
					instance_id: "session-1",
					asset_ids: [initialEsm.id, initialCss.id, launchedEsm.id, launchedCss.id],
				},
			},
			undefined,
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
		const initialized: Array<Record<string, unknown>> = [];
		const names: string[] = [];
		const callServerTool = vi.fn(
			async (request: { name: string; arguments?: Record<string, unknown> }) => {
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
			},
		);
		const app = {
			callServerTool,
			getHostCapabilities: () => ({}),
			updateModelContext: vi.fn().mockResolvedValue({}),
		} as unknown as App;
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
				(request.arguments?.asset_ids as string[] | undefined)?.includes(hotEsm.id),
		)?.[0];
		expect(new Set(dynamicAssetRequest?.arguments?.asset_ids as string[])).toEqual(
			new Set([hotEsm.id, childCss.id, hotCss.id]),
		);

		await runtime.dispose();
	});

	test("rejects an unversioned launch and disposes its server session", async () => {
		const app = {
			callServerTool: vi.fn().mockResolvedValue({ content: [] }),
			getHostCapabilities: () => ({}),
			updateModelContext: vi.fn().mockResolvedValue({}),
		} as unknown as App;

		await expect(
			WidgetRuntime.create(
				{
					instanceId: "session-1",
					rootModelId: "root-model",
					assetManifest: {},
					models: {},
				},
				new ToolCallQueue(app),
				app,
				Promise.resolve(),
			),
		).rejects.toThrow("Widget payload protocol version undefined is incompatible with version 1");
		expect(app.callServerTool).toHaveBeenCalledOnce();
		expect(app.callServerTool).toHaveBeenCalledWith(
			{
				name: "anywidget_dispose",
				arguments: { instance_id: "session-1" },
			},
			{ signal: expect.any(AbortSignal) },
		);
	});

	test("rejects a launch without a root model and disposes its server session", async () => {
		const app = {
			callServerTool: vi.fn().mockResolvedValue({ content: [] }),
			getHostCapabilities: () => ({}),
			updateModelContext: vi.fn().mockResolvedValue({}),
		} as unknown as App;

		await expect(
			WidgetRuntime.create(
				{
					protocolVersion: 1,
					instanceId: "missing-root-session",
					assetManifest: {},
					models: {},
				},
				new ToolCallQueue(app),
				app,
				Promise.resolve(),
			),
		).rejects.toThrow("Missing root model ID");
		expect(app.callServerTool).toHaveBeenCalledOnce();
		expect(app.callServerTool).toHaveBeenCalledWith(
			{
				name: "anywidget_dispose",
				arguments: { instance_id: "missing-root-session" },
			},
			{ signal: expect.any(AbortSignal) },
		);
	});

	test("rejects inline launch sources and disposes their server session", async () => {
		const app = {
			callServerTool: vi.fn().mockResolvedValue({ content: [] }),
			getHostCapabilities: () => ({}),
			updateModelContext: vi.fn().mockResolvedValue({}),
		} as unknown as App;

		await expect(
			WidgetRuntime.create(
				{
					protocolVersion: 1,
					instanceId: "inline-session",
					rootModelId: "root-model",
					assetManifest: {},
					models: {
						"root-model": {
							state: { _esm: "export default { render() {} }" },
						},
					},
				},
				new ToolCallQueue(app),
				app,
				Promise.resolve(),
			),
		).rejects.toThrow("Widget source _esm must use a content-addressed reference");
		expect(app.callServerTool).toHaveBeenCalledOnce();
		expect(app.callServerTool).toHaveBeenCalledWith(
			{
				name: "anywidget_dispose",
				arguments: { instance_id: "inline-session" },
			},
			{ signal: expect.any(AbortSignal) },
		);
	});

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
			} as unknown as App;
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
			const creationError = creation.catch((error: unknown) => error);

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
		} as unknown as App;
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
		} as unknown as App;
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
		const callServerTool = vi.fn(
			async (request: { name: string; arguments?: Record<string, unknown> }) => {
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
			},
		);
		const app = {
			callServerTool,
			getHostCapabilities: () => ({}),
			updateModelContext: vi.fn().mockResolvedValue({}),
		} as unknown as App;

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
		const disposeCalls = callServerTool.mock.calls.filter(
			([request]) => request.name === "anywidget_dispose",
		);
		expect(disposeCalls).toHaveLength(1);
		expect(disposeCalls[0]?.[0]).toEqual({
			name: "anywidget_dispose",
			arguments: { instance_id: "failed-session" },
		});
	});

	test("rejects inline source traits in dynamic messages", async () => {
		const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
		const initialEsm = await fixtureAsset("esm", "export default { render() {} }");
		const assets = new Map([[initialEsm.id, initialEsm]]);
		const callServerTool = vi.fn(
			async (request: { name: string; arguments?: Record<string, unknown> }) => {
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
			},
		);
		const app = {
			callServerTool,
			getHostCapabilities: () => ({}),
			updateModelContext: vi.fn().mockResolvedValue({}),
		} as unknown as App;
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
