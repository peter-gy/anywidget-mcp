import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { afterEach, describe, expect, test, vi } from "vite-plus/test";

import { WidgetRuntime } from "../src/app";
import { blobRef, type BlobRef } from "../src/attachments";
import { readResult, writeResult } from "./attachment-test-support";
import { isRecord } from "../src/runtime-value";
import type { ToolArguments } from "../src/tool-calls";
import type { RuntimeBinding } from "../src/binding";
import type { BridgeModel } from "../src/model";
import type { RuntimeRecord } from "../src/runtime-value";
import { ToolCallQueue, type ToolRequest } from "../src/tool-calls";
import { fixtureStrings } from "./runtime-test-support";

interface FixtureAsset {
	id: string;
	ref: BlobRef;
	text: string;
	byteLength: number;
}

async function fixtureAsset(text: string): Promise<FixtureAsset> {
	const ref = await blobRef(new TextEncoder().encode(text));
	return { id: ref.id, ref, text, byteLength: ref.byteLength };
}

function assetContents(
	assets: ReadonlyMap<string, FixtureAsset>,
	args: ToolArguments = {},
): CallToolResult {
	const asset = assets.get(String(args.blob_id));
	if (!asset) throw new Error("unknown fixture source");
	return readResult(new TextEncoder().encode(asset.text), args);
}

function delivered(result: CallToolResult): CallToolResult {
	const payload = result._meta?.anywidget;
	if (!isRecord(payload)) throw new Error("missing fixture payload");
	return {
		...result,
		_meta: { anywidget: { protocolVersion: 3, instanceId: "session-1", payload } },
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
		protocolVersion: 3,
		instanceId: "invalid-session",
		rootModelId: "root-model",

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
		"Widget payload protocol version undefined is incompatible with version 3",
	],
	[
		"an incompatible protocol version",
		runtimePayload({ protocolVersion: 4 }),
		"Widget payload protocol version 4 is incompatible with version 3",
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
];

describe("WidgetRuntime asset hydration", () => {
	afterEach(() => {
		vi.unstubAllGlobals();
		vi.restoreAllMocks();
	});

	test("hydrates initial models and launch source updates before exposing the runtime", async () => {
		const initialEsm = await fixtureAsset("\nexport default { render() {} }\n");
		const initialCss = await fixtureAsset(".initial { color: red; }\n");
		const launchedEsm = await fixtureAsset("\nexport default { render() { /* launch λ */ } }\n");
		const launchedCss = await fixtureAsset('[data-state="launch"]::after { content: "✓"; }\n');
		const assets = new Map(
			[initialEsm, initialCss, launchedEsm, launchedCss].map((asset) => [asset.id, asset]),
		);
		const callServerTool = vi.fn(async (request: ToolRequest) => {
			if (request.name === "anywidget_read") {
				return assetContents(assets, request.arguments);
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
				protocolVersion: 3,
				instanceId: "session-1",
				rootModelId: "root-model",

				models: {
					"root-model": {
						modelId: "root-model",
						state: { value: 0 },
						sourceRefs: { _esm: initialEsm.ref, _css: initialCss.ref },
					},
				},
				messages: [
					{
						modelId: "root-model",
						data: { method: "update", state: { value: 1 } },
						sourceRefs: { _esm: launchedEsm.ref, _css: launchedCss.ref },
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
			([request]) => request.name === "anywidget_read",
		)?.[0];
		expect(assetRequest).toMatchObject({
			name: "anywidget_read",
			arguments: { instance_id: "session-1" },
		});
		expect(
			callServerTool.mock.calls.filter(([request]) => request.name === "anywidget_read"),
		).toHaveLength(4);

		await runtime.dispose();
	});

	test("hydrates dynamic models and hot ESM and CSS inside the active comm transaction", async () => {
		const initialEsm = await fixtureAsset("export default { render() {} }");
		const initialCss = await fixtureAsset(".initial {}");
		const hotEsm = await fixtureAsset("\nexport default { render() { /* hot café */ } }\n");
		const hotCss = await fixtureAsset('[data-hot="true"] { content: "λ"; }\n');
		const childCss = await fixtureAsset(".child { color: rgb(1, 2, 3); }\n");
		const assets = new Map(
			[initialEsm, initialCss, hotEsm, hotCss, childCss].map((asset) => [asset.id, asset]),
		);
		const initialized: RuntimeRecord[] = [];
		const names: string[] = [];
		const callServerTool = vi.fn(async (request: ToolRequest) => {
			names.push(request.name);
			if (request.name === "anywidget_read") {
				return assetContents(assets, request.arguments);
			}
			if (request.name === "anywidget_write") return writeResult(request.arguments ?? {});
			if (request.name === "anywidget_comm") {
				return delivered({
					content: [],
					_meta: {
						anywidget: {
							protocolVersion: 3,

							models: {
								"child-model": {
									modelId: "child-model",
									state: { value: 9 },
									sourceRefs: { _esm: hotEsm.ref, _css: childCss.ref },
								},
							},
							messages: [
								{
									modelId: "root-model",
									data: { method: "update", state: { value: 2 } },
									sourceRefs: { _esm: hotEsm.ref, _css: hotCss.ref },
									buffers: [],
								},
							],
						},
					},
				} satisfies CallToolResult);
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
				protocolVersion: 3,
				instanceId: "session-1",
				rootModelId: "root-model",

				models: {
					"root-model": {
						modelId: "root-model",
						state: { value: 0 },
						sourceRefs: { _esm: initialEsm.ref, _css: initialCss.ref },
					},
				},
			},
			calls,
			app,
			Promise.resolve(),
			(_bindingRuntime, model) => new RecordingBinding(model, initialized),
		);

		await runtime.send("root-model", { method: "update", state: { value: 1 } }, []);

		expect(names).toEqual([
			"anywidget_read",
			"anywidget_read",
			"anywidget_write",
			"anywidget_comm",
			"anywidget_read",
			"anywidget_read",
			"anywidget_read",
		]);
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
					message: "Widget payload protocol version undefined is incompatible with version 3",
				}),
			);
		} finally {
			vi.useRealTimers();
		}
	});

	test("aborts stalled source loading and disposes its server session", async () => {
		const esm = await fixtureAsset("export default { render() {} }");
		const names: string[] = [];
		const callServerTool = vi.fn(async (request: { name: string }): Promise<CallToolResult> => {
			names.push(request.name);
			if (request.name === "anywidget_read") {
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
				protocolVersion: 3,
				instanceId: "cancelled-session",
				rootModelId: "root-model",

				models: {
					"root-model": {
						state: {},
						sourceRefs: { _esm: esm.ref },
					},
				},
			},
			new ToolCallQueue(app),
			app,
			Promise.resolve(),
			undefined,
			controller.signal,
		);

		await vi.waitFor(() => expect(names).toEqual(["anywidget_read"]));
		controller.abort(new DOMException("teardown", "AbortError"));

		await expect(creation).rejects.toMatchObject({ name: "AbortError" });
		expect(names).toEqual(["anywidget_read", "anywidget_dispose"]);
	});

	test("aborts stalled persistent cache reads and disposes the server session", async () => {
		const esm = await fixtureAsset("export default { render() {} }");
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
				protocolVersion: 3,
				instanceId: "stalled-cache-session",
				rootModelId: "root-model",

				models: {
					"root-model": {
						state: {},
						sourceRefs: { _esm: esm.ref },
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
		const esm = await fixtureAsset("export default { render() {} }");
		const names: string[] = [];
		const callServerTool = vi.fn(async (request: ToolRequest) => {
			names.push(request.name);
			if (request.name === "anywidget_read") {
				return readResult(
					new TextEncoder().encode("x".repeat(esm.byteLength)),
					request.arguments ?? {},
				);
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
					protocolVersion: 3,
					instanceId: "failed-session",
					rootModelId: "root-model",

					models: {
						"root-model": {
							modelId: "root-model",
							state: {},
							sourceRefs: { _esm: esm.ref },
						},
					},
				},
				new ToolCallQueue(app),
				app,
				Promise.resolve(),
			),
		).rejects.toThrow(`Widget attachment failed verification for ${esm.id}`);

		expect(names).toEqual(["anywidget_read", "anywidget_dispose"]);
		expect(callServerTool.mock.calls.at(-1)?.[0]).toEqual({
			name: "anywidget_dispose",
			arguments: { session_id: "failed-session" },
		});
	});

	test("rejects inline source traits in dynamic messages", async () => {
		const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
		const initialEsm = await fixtureAsset("export default { render() {} }");
		const assets = new Map([[initialEsm.id, initialEsm]]);
		const callServerTool = vi.fn(async (request: ToolRequest) => {
			if (request.name === "anywidget_read") {
				return assetContents(assets, request.arguments);
			}
			if (request.name === "anywidget_write") return writeResult(request.arguments ?? {});
			if (request.name === "anywidget_comm") {
				return delivered({
					content: [],
					_meta: {
						anywidget: {
							protocolVersion: 3,

							messages: [
								{
									modelId: "root-model",
									data: { method: "update", state: { _css: ".inline {}" } },
									buffers: [],
								},
							],
						},
					},
				} satisfies CallToolResult);
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
				protocolVersion: 3,
				instanceId: "session-1",
				rootModelId: "root-model",

				models: {
					"root-model": {
						state: {},
						sourceRefs: { _esm: initialEsm.ref },
					},
				},
			},
			new ToolCallQueue(app),
			app,
			Promise.resolve(),
			(_runtime, model) => new RecordingBinding(model, []),
		);

		await expect(runtime.send("root-model", { method: "update", state: {} }, [])).rejects.toThrow(
			"Widget source _css must use a content-addressed reference",
		);
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
