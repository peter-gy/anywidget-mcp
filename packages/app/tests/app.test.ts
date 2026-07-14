import type { App } from "@modelcontextprotocol/ext-apps";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { describe, expect, test, vi } from "vite-plus/test";

import { WidgetRuntime } from "../src/app";
import { WidgetBinding, type RuntimeBinding, type WidgetDefinition } from "../src/binding";
import type { BridgeModel } from "../src/model";
import { ToolCallQueue } from "../src/tool-calls";

type ToolCall = (
	name: string,
	args: Record<string, unknown>,
	signal?: AbortSignal,
) => Promise<CallToolResult>;

function fakeQueue(call: ToolCall, callNow: ToolCall): ToolCallQueue {
	return {
		call,
		callNow,
		transaction: <T>(
			task: (
				call: (name: string, args: Record<string, unknown>) => Promise<CallToolResult>,
			) => Promise<T>,
			signal?: AbortSignal,
		): Promise<T> => {
			signal?.throwIfAborted();
			return task((name, args) => call(name, args, signal));
		},
	} as unknown as ToolCallQueue;
}

function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((done) => {
		resolve = done;
	});
	return { promise, resolve };
}

class FakeBinding implements RuntimeBinding {
	private readonly ready: Promise<void>;
	private resolveReady!: () => void;

	constructor(
		readonly model: BridgeModel,
		private readonly events: string[],
	) {
		this.ready = new Promise<void>((resolve) => {
			this.resolveReady = resolve;
		});
	}

	async initialize(): Promise<void> {
		this.events.push(`initialize:${this.model.modelId}`);
		this.resolveReady();
	}

	async render(): Promise<void> {}

	async getExports(): Promise<unknown> {
		await this.ready;
		return { modelId: this.model.modelId };
	}

	async dispose(): Promise<void> {
		this.events.push(`dispose:${this.model.modelId}`);
	}
}

describe("WidgetRuntime model graph updates", () => {
	test("applies the atomic launch snapshot before widget lifecycle and context delivery", async () => {
		vi.useFakeTimers();
		try {
			const events: string[] = [];
			const custom: Array<{ content: unknown; phase: string }> = [];
			let phase = "before";
			const updateModelContext = vi.fn(() => {
				events.push("context");
				return Promise.resolve({});
			});
			const callServerTool = vi.fn().mockResolvedValue({ content: [] });
			const app = {
				getHostCapabilities: () => ({ updateModelContext: { structuredContent: {} } }),
				updateModelContext,
				callServerTool,
			} as unknown as App;
			const loadWidget = vi.fn(async (source: string): Promise<WidgetDefinition> => {
				events.push(`load:${source}`);
				return {
					initialize({ model }) {
						phase = "initialize";
						events.push(`initialize:${String(model.get("_esm"))}:${String(model.get("value"))}`);
						model.on("msg:custom", (content) => {
							custom.push({ content, phase });
							events.push(`custom:${phase}`);
						});
						phase = "initialized";
					},
					render({ model }) {
						phase = "render";
						events.push(`render:${String(model.get("_esm"))}:${String(model.get("value"))}`);
					},
				};
			});
			const replaceCss = vi.fn().mockResolvedValue(undefined);
			const runtime = new WidgetRuntime(
				{
					instanceId: "instance-1",
					rootModelId: "root-model",
					models: {
						"root-model": {
							modelId: "root-model",
							state: { _esm: "source-v0", _css: ".v0 {}", value: 0 },
						},
					},
					messages: [
						{
							modelId: "root-model",
							data: {
								method: "update",
								state: { _esm: "source-v1", _css: ".v1 {}", value: 1 },
							},
							buffers: [],
						},
						{
							modelId: "root-model",
							data: { method: "custom", content: { event: "launched" } },
							buffers: [],
						},
					],
					context: {
						version: 1,
						tool: "example.Widget",
						state: { value: 1 },
					},
				},
				new ToolCallQueue(app),
				app,
				Promise.resolve(),
				(bindingRuntime, model) =>
					new WidgetBinding(bindingRuntime, model, {
						reportError: vi.fn(),
						loadWidget,
						replaceCss,
					}),
			);
			const element = { replaceChildren: vi.fn() } as unknown as HTMLElement;

			await runtime.mount(element);

			expect(loadWidget.mock.calls.map(([source]) => source)).toEqual(["source-v1"]);
			expect(replaceCss.mock.calls.map(([css]) => css)).toEqual([".v1 {}"]);
			expect(custom).toEqual([{ content: { event: "launched" }, phase: "initialize" }]);
			expect(events).toEqual([
				"load:source-v1",
				"initialize:source-v1:1",
				"custom:initialize",
				"render:source-v1:1",
			]);
			expect(updateModelContext).not.toHaveBeenCalled();

			await vi.advanceTimersByTimeAsync(250);

			expect(events.at(-1)).toBe("context");
			expect(updateModelContext).toHaveBeenCalledWith(
				{ structuredContent: { tool: "example.Widget", state: { value: 1 } } },
				{ signal: expect.any(AbortSignal) },
			);
			await runtime.dispose();
		} finally {
			vi.useRealTimers();
		}
	});

	test("rejects a launch message for a model outside the final graph", () => {
		const app = {
			getHostCapabilities: () => ({}),
			updateModelContext: vi.fn().mockResolvedValue({}),
			callServerTool: vi.fn().mockResolvedValue({ content: [] }),
		} as unknown as App;

		expect(
			() =>
				new WidgetRuntime(
					{
						instanceId: "instance-1",
						rootModelId: "root-model",
						models: {
							"root-model": {
								modelId: "root-model",
								state: { _esm: "source-v0" },
							},
						},
						messages: [
							{
								modelId: "missing-model",
								data: { method: "update", state: { value: 1 } },
								buffers: [],
							},
						],
					},
					new ToolCallQueue(app),
					app,
					Promise.resolve(),
					(_runtime, model) => new FakeBinding(model, []),
				),
		).toThrow("Widget launch message references unknown model missing-model");
	});

	test("applies parent messages before initializing added models and removes old models after", async () => {
		const events: string[] = [];
		const rootId = "root-model";
		const oldId = "old-child";
		const newId = "new-child";
		const update: CallToolResult = {
			content: [],
			_meta: {
				anywidget: {
					models: {
						[newId]: {
							modelId: newId,
							state: { _esm: "export default {}", value: 9 },
							bufferPaths: [],
							buffers: [],
						},
					},
					removedModelIds: [oldId],
					messages: [
						{
							modelId: rootId,
							data: {
								method: "update",
								state: { child: `anywidget:${newId}` },
							},
							buffers: [],
						},
					],
				},
			},
		};
		const call = vi.fn(
			async (
				name: string,
				_args: Record<string, unknown>,
				_signal?: AbortSignal,
			): Promise<CallToolResult> => (name === "anywidget_comm" ? update : { content: [] }),
		);
		const callNow = vi.fn().mockResolvedValue({ content: [] });
		const calls = fakeQueue(call, callNow);
		const app = {
			getHostCapabilities: () => ({}),
			updateModelContext: vi.fn().mockResolvedValue({}),
		} as unknown as App;
		const runtime = new WidgetRuntime(
			{
				instanceId: "instance-1",
				rootModelId: rootId,
				models: {
					[rootId]: {
						modelId: rootId,
						state: { _esm: "export default {}", child: `anywidget:${oldId}` },
					},
					[oldId]: {
						modelId: oldId,
						state: { _esm: "export default {}", value: 7 },
					},
				},
			},
			calls,
			app,
			Promise.resolve(),
			(_runtime, model) => new FakeBinding(model, events),
		);
		const root = runtime.model(rootId);
		let resolvedWidget: Promise<void> | undefined;
		root.on("change:child", () => {
			events.push("message:parent");
			expect(runtime.model(oldId).modelId).toBe(oldId);
			const ref = String(root.get("child"));
			resolvedWidget = runtime
				.host(new AbortController().signal)
				.getWidget(ref)
				.then(() => {
					events.push("resolved:new-child");
				});
		});

		await runtime.send(rootId, { method: "update", state: {} }, [], "operation-1");
		await resolvedWidget;

		expect(events.indexOf("message:parent")).toBeLessThan(events.indexOf(`initialize:${newId}`));
		expect(events.indexOf(`initialize:${newId}`)).toBeLessThan(events.indexOf(`dispose:${oldId}`));
		expect(events).toContain("resolved:new-child");
		expect(() => runtime.model(oldId)).toThrow("Unknown anywidget model");
		expect(runtime.model(newId).get("value")).toBe(9);
		expect(call).toHaveBeenCalledWith(
			"anywidget_comm",
			{
				instance_id: "instance-1",
				model_id: rootId,
				data: { method: "update", state: {} },
				buffers: [],
				operation_id: "operation-1",
			},
			expect.any(AbortSignal),
		);

		const commSignal = call.mock.calls[0]?.[2];
		expect(commSignal).toBeInstanceOf(AbortSignal);
		await runtime.dispose();
		expect(callNow).toHaveBeenCalledWith(
			"anywidget_dispose",
			{ instance_id: "instance-1" },
			expect.any(AbortSignal),
		);
		expect(callNow.mock.calls[0]?.[2]).not.toBe(commSignal);
	});

	test("keeps retries and response application inside one global queue slot", async () => {
		vi.useFakeTimers();
		try {
			const rootId = "root-model";
			const addedId = "added-model";
			const processing = deferred<void>();
			const events: string[] = [];
			let firstAttempts = 0;
			const update: CallToolResult = {
				content: [],
				_meta: {
					anywidget: {
						models: {
							[addedId]: {
								modelId: addedId,
								state: { _esm: "export default {}" },
							},
						},
						messages: [],
					},
				},
			};
			const callServerTool = vi.fn(
				async (request: {
					name: string;
					arguments?: Record<string, unknown>;
				}): Promise<CallToolResult> => {
					if (request.name === "anywidget_dispose") return { content: [] };
					const operationId = request.arguments?.operation_id;
					if (operationId === "operation-1") {
						firstAttempts += 1;
						if (firstAttempts === 1) throw new Error("response lost");
						return update;
					}
					return { content: [] };
				},
			);
			const calls = new ToolCallQueue({ callServerTool } as unknown as App);
			const runtime = new WidgetRuntime(
				{
					instanceId: "instance-1",
					rootModelId: rootId,
					models: {
						[rootId]: {
							modelId: rootId,
							state: { _esm: "export default {}" },
						},
					},
				},
				calls,
				{
					getHostCapabilities: () => ({}),
					updateModelContext: vi.fn().mockResolvedValue({}),
				} as unknown as App,
				Promise.resolve(),
				(_runtime, model): RuntimeBinding => ({
					async initialize() {
						if (model.modelId !== addedId) return;
						events.push("processing:first-result");
						await processing.promise;
					},
					async render() {},
					async getExports() {},
					async dispose() {},
				}),
			);

			const first = runtime.send(
				rootId,
				{ method: "update", state: { value: 1 } },
				[],
				"operation-1",
			);
			const second = runtime.send(
				rootId,
				{ method: "update", state: { value: 2 } },
				[],
				"operation-2",
			);
			await vi.advanceTimersByTimeAsync(100);

			expect(events).toEqual(["processing:first-result"]);
			const dispatchedBeforeProcessing = callServerTool.mock.calls
				.filter(([request]) => request.name === "anywidget_comm")
				.map(([request]) => request.arguments?.operation_id);
			expect(dispatchedBeforeProcessing).toEqual(["operation-1", "operation-1"]);

			processing.resolve(undefined);
			await Promise.all([first, second]);
			const dispatched = callServerTool.mock.calls
				.filter(([request]) => request.name === "anywidget_comm")
				.map(([request]) => request.arguments?.operation_id);
			expect(dispatched).toEqual(["operation-1", "operation-1", "operation-2"]);
			await runtime.dispose();
		} finally {
			vi.useRealTimers();
		}
	});

	test("aborts the runtime when idempotent transport delivery is exhausted", async () => {
		vi.useFakeTimers();
		const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
		try {
			const callServerTool = vi.fn(
				async (request: {
					name: string;
					arguments?: Record<string, unknown>;
				}): Promise<CallToolResult> => {
					if (request.name === "anywidget_dispose") return { content: [] };
					throw new Error("offline");
				},
			);
			const runtime = new WidgetRuntime(
				{
					instanceId: "instance-1",
					rootModelId: "root-model",
					models: {
						"root-model": {
							modelId: "root-model",
							state: { _esm: "export default {}" },
						},
					},
				},
				new ToolCallQueue({ callServerTool } as unknown as App),
				{
					getHostCapabilities: () => ({}),
					updateModelContext: vi.fn().mockResolvedValue({}),
				} as unknown as App,
				Promise.resolve(),
				(_runtime, model) => new FakeBinding(model, []),
			);

			const failed = runtime.send(
				"root-model",
				{ method: "update", state: { value: 1 } },
				[],
				"operation-1",
			);
			const queued = runtime.send(
				"root-model",
				{ method: "update", state: { value: 2 } },
				[],
				"operation-2",
			);
			const outcomesPromise = Promise.allSettled([failed, queued]);
			await vi.runAllTimersAsync();
			const outcomes = await outcomesPromise;
			expect(outcomes.map((outcome) => outcome.status)).toEqual(["rejected", "rejected"]);

			const requests = callServerTool.mock.calls.map(([request]) => request);
			expect(
				requests
					.filter((request) => request.name === "anywidget_comm")
					.map((request) => request.arguments?.operation_id),
			).toEqual(["operation-1", "operation-1", "operation-1"]);
			expect(requests.filter((request) => request.name === "anywidget_dispose")).toHaveLength(1);

			await runtime.send(
				"root-model",
				{ method: "update", state: { value: 3 } },
				[],
				"operation-3",
			);
			expect(callServerTool).toHaveBeenCalledTimes(4);
		} finally {
			consoleError.mockRestore();
			vi.useRealTimers();
		}
	});

	test("bypasses a stalled poll when disposing the server session", async () => {
		vi.useFakeTimers();
		try {
			const poll = new Promise<CallToolResult>(() => undefined);
			const callServerTool = vi.fn(async (request: { name: string }): Promise<CallToolResult> => {
				if (request.name === "anywidget_poll") return poll;
				return { content: [] };
			});
			const app = {
				getHostCapabilities: () => ({}),
				updateModelContext: vi.fn().mockResolvedValue({}),
			} as unknown as App;
			const runtime = new WidgetRuntime(
				{
					instanceId: "instance-1",
					rootModelId: "root-model",
					models: {
						"root-model": {
							modelId: "root-model",
							state: { _esm: "export default {}" },
						},
					},
				},
				new ToolCallQueue({ callServerTool } as unknown as App),
				app,
				Promise.resolve(),
				(_runtime, model) => new FakeBinding(model, []),
			);

			await runtime.mount({} as HTMLElement);
			await vi.advanceTimersByTimeAsync(500);
			expect(callServerTool).toHaveBeenCalledWith(
				{
					name: "anywidget_poll",
					arguments: { instance_id: "instance-1", operation_id: expect.any(String) },
				},
				{ signal: expect.any(AbortSignal) },
			);

			await expect(runtime.dispose()).resolves.toBeUndefined();
			expect(callServerTool).toHaveBeenCalledWith(
				{
					name: "anywidget_dispose",
					arguments: { instance_id: "instance-1" },
				},
				{ signal: expect.any(AbortSignal) },
			);
		} finally {
			vi.useRealTimers();
		}
	});

	test("backs off polling while the widget session stays idle", async () => {
		vi.useFakeTimers();
		try {
			const callServerTool = vi.fn().mockResolvedValue({ content: [] });
			const runtime = new WidgetRuntime(
				{
					instanceId: "instance-1",
					rootModelId: "root-model",
					models: {
						"root-model": {
							modelId: "root-model",
							state: { _esm: "export default {}" },
						},
					},
				},
				new ToolCallQueue({ callServerTool } as unknown as App),
				{
					getHostCapabilities: () => ({}),
					updateModelContext: vi.fn().mockResolvedValue({}),
				} as unknown as App,
				Promise.resolve(),
				(_runtime, model) => new FakeBinding(model, []),
			);
			const pollCount = (): number =>
				callServerTool.mock.calls.filter(([request]) => request.name === "anywidget_poll").length;
			const advanceToPoll = async (delay: number, expectedCount: number): Promise<void> => {
				await vi.advanceTimersByTimeAsync(delay - 1);
				expect(pollCount()).toBe(expectedCount - 1);
				await vi.advanceTimersByTimeAsync(1);
				expect(pollCount()).toBe(expectedCount);
			};

			await runtime.mount({} as HTMLElement);
			await advanceToPoll(500, 1);
			await advanceToPoll(1000, 2);
			await advanceToPoll(2000, 3);
			await advanceToPoll(5000, 4);
			await advanceToPoll(10_000, 5);
			await advanceToPoll(15_000, 6);
			await advanceToPoll(15_000, 7);

			await runtime.dispose();
		} finally {
			vi.useRealTimers();
		}
	});

	test("resets polling to the active delay after server activity", async () => {
		vi.useFakeTimers();
		try {
			let pollCount = 0;
			const callServerTool = vi.fn(async (request: { name: string }): Promise<CallToolResult> => {
				if (request.name !== "anywidget_poll") return { content: [] };
				pollCount += 1;
				if (pollCount !== 2) return { content: [] };
				return {
					content: [],
					_meta: {
						anywidget: {
							messages: [
								{
									modelId: "root-model",
									data: { method: "echo_update" },
									buffers: [],
								},
							],
						},
					},
				};
			});
			const runtime = new WidgetRuntime(
				{
					instanceId: "instance-1",
					rootModelId: "root-model",
					models: {
						"root-model": {
							modelId: "root-model",
							state: { _esm: "export default {}" },
						},
					},
				},
				new ToolCallQueue({ callServerTool } as unknown as App),
				{
					getHostCapabilities: () => ({}),
					updateModelContext: vi.fn().mockResolvedValue({}),
				} as unknown as App,
				Promise.resolve(),
				(_runtime, model) => new FakeBinding(model, []),
			);

			await runtime.mount({} as HTMLElement);
			await vi.advanceTimersByTimeAsync(500);
			expect(pollCount).toBe(1);
			await vi.advanceTimersByTimeAsync(999);
			expect(pollCount).toBe(1);
			await vi.advanceTimersByTimeAsync(1);
			expect(pollCount).toBe(2);
			await vi.advanceTimersByTimeAsync(499);
			expect(pollCount).toBe(2);
			await vi.advanceTimersByTimeAsync(1);
			expect(pollCount).toBe(3);

			await runtime.dispose();
		} finally {
			vi.useRealTimers();
		}
	});

	test("wakes an idle poll after a browser comm", async () => {
		vi.useFakeTimers();
		try {
			const callServerTool = vi.fn().mockResolvedValue({ content: [] });
			const runtime = new WidgetRuntime(
				{
					instanceId: "instance-1",
					rootModelId: "root-model",
					models: {
						"root-model": {
							modelId: "root-model",
							state: { _esm: "export default {}" },
						},
					},
				},
				new ToolCallQueue({ callServerTool } as unknown as App),
				{
					getHostCapabilities: () => ({}),
					updateModelContext: vi.fn().mockResolvedValue({}),
				} as unknown as App,
				Promise.resolve(),
				(_runtime, model) => new FakeBinding(model, []),
			);
			const pollCount = (): number =>
				callServerTool.mock.calls.filter(([request]) => request.name === "anywidget_poll").length;

			await runtime.mount({} as HTMLElement);
			await vi.advanceTimersByTimeAsync(500);
			await vi.advanceTimersByTimeAsync(1000);
			await vi.advanceTimersByTimeAsync(2000);
			await vi.advanceTimersByTimeAsync(5000);
			await vi.advanceTimersByTimeAsync(10_000);
			expect(pollCount()).toBe(5);

			await runtime.send(
				"root-model",
				{ method: "custom", content: { kind: "schedule-python-update" } },
				[],
				"comm-after-idle",
			);
			await vi.advanceTimersByTimeAsync(499);
			expect(pollCount()).toBe(5);
			await vi.advanceTimersByTimeAsync(1);
			expect(pollCount()).toBe(6);

			await runtime.dispose();
		} finally {
			vi.useRealTimers();
		}
	});

	test("retries a poll with one operation ID before later comms", async () => {
		vi.useFakeTimers();
		try {
			let pollAttempts = 0;
			const callServerTool = vi.fn(
				async (request: {
					name: string;
					arguments?: Record<string, unknown>;
				}): Promise<CallToolResult> => {
					if (request.name === "anywidget_poll") {
						pollAttempts += 1;
						if (pollAttempts === 1) throw new Error("poll response lost");
					}
					return { content: [] };
				},
			);
			const runtime = new WidgetRuntime(
				{
					instanceId: "instance-1",
					rootModelId: "root-model",
					models: {
						"root-model": {
							modelId: "root-model",
							state: { _esm: "export default {}" },
						},
					},
				},
				new ToolCallQueue({ callServerTool } as unknown as App),
				{
					getHostCapabilities: () => ({}),
					updateModelContext: vi.fn().mockResolvedValue({}),
				} as unknown as App,
				Promise.resolve(),
				(_runtime, model) => new FakeBinding(model, []),
			);
			await runtime.mount({} as HTMLElement);
			await vi.advanceTimersByTimeAsync(500);

			const comm = runtime.send(
				"root-model",
				{ method: "custom", content: { kind: "ping" } },
				[],
				"comm-operation",
			);
			await vi.advanceTimersByTimeAsync(100);
			await comm;

			const requests = callServerTool.mock.calls
				.map(([request]) => request)
				.filter((request) => request.name !== "anywidget_dispose");
			expect(requests.map((request) => request.name)).toEqual([
				"anywidget_poll",
				"anywidget_poll",
				"anywidget_comm",
			]);
			const pollOperationIds = requests
				.filter((request) => request.name === "anywidget_poll")
				.map((request) => request.arguments?.operation_id);
			expect(pollOperationIds[0]).toEqual(expect.any(String));
			expect(pollOperationIds[1]).toBe(pollOperationIds[0]);
			await runtime.dispose();
		} finally {
			vi.useRealTimers();
		}
	});

	test("preserves API call order across models and coalesces adjacent updates", async () => {
		const first = deferred<CallToolResult>();
		const callServerTool = vi
			.fn()
			.mockReturnValueOnce(first.promise)
			.mockResolvedValue({ content: [] });
		const runtime = new WidgetRuntime(
			{
				instanceId: "instance-1",
				rootModelId: "model-a",
				models: {
					"model-a": {
						modelId: "model-a",
						state: { _esm: "export default {}", value: -1 },
					},
					"model-b": {
						modelId: "model-b",
						state: { _esm: "export default {}", value: -1 },
					},
				},
			},
			new ToolCallQueue({ callServerTool } as unknown as App),
			{
				getHostCapabilities: () => ({}),
				updateModelContext: vi.fn().mockResolvedValue({}),
			} as unknown as App,
			Promise.resolve(),
			(_runtime, model) => new FakeBinding(model, []),
		);
		const modelA = runtime.model("model-a");
		const modelB = runtime.model("model-b");

		modelA.set("value", 0);
		modelA.save_changes();
		for (let value = 1; value <= 100; value += 1) {
			modelA.set("value", value);
			modelA.save_changes();
		}
		modelA.send({ kind: "barrier" });
		modelB.set("value", 7);
		modelB.save_changes();

		await vi.waitFor(() => expect(callServerTool).toHaveBeenCalledTimes(1));
		first.resolve({ content: [] });
		await vi.waitFor(() =>
			expect(
				callServerTool.mock.calls.filter(([request]) => request.name === "anywidget_comm"),
			).toHaveLength(4),
		);

		const comms = callServerTool.mock.calls
			.map(([request]) => request)
			.filter((request) => request.name === "anywidget_comm")
			.map((request) => request.arguments);
		expect(comms.map((args) => args?.model_id)).toEqual([
			"model-a",
			"model-a",
			"model-a",
			"model-b",
		]);
		expect(comms.map((args) => (args?.data as { method?: string } | undefined)?.method)).toEqual([
			"update",
			"update",
			"custom",
			"update",
		]);
		expect((comms[1]?.data as { state?: unknown } | undefined)?.state).toEqual({ value: 100 });

		await runtime.dispose();
	});

	test("keeps queued custom messages ahead of a later poll", async () => {
		vi.useFakeTimers();
		try {
			const first = deferred<CallToolResult>();
			const callServerTool = vi
				.fn()
				.mockReturnValueOnce(first.promise)
				.mockResolvedValue({ content: [] });
			const runtime = new WidgetRuntime(
				{
					instanceId: "instance-1",
					rootModelId: "root-model",
					models: {
						"root-model": {
							modelId: "root-model",
							state: { _esm: "export default {}", value: 0 },
						},
					},
				},
				new ToolCallQueue({ callServerTool } as unknown as App),
				{
					getHostCapabilities: () => ({}),
					updateModelContext: vi.fn().mockResolvedValue({}),
				} as unknown as App,
				Promise.resolve(),
				(_runtime, model) => new FakeBinding(model, []),
			);
			await runtime.mount({} as HTMLElement);
			const model = runtime.model("root-model");
			model.set("value", 1);
			model.save_changes();
			model.send({ kind: "before-poll" });
			await vi.advanceTimersByTimeAsync(500);

			first.resolve({ content: [] });
			await vi.waitFor(() => expect(callServerTool).toHaveBeenCalledTimes(3));
			const requests = callServerTool.mock.calls.map(([request]) => request);
			expect(requests.map((request) => request.name)).toEqual([
				"anywidget_comm",
				"anywidget_comm",
				"anywidget_poll",
			]);
			expect(
				requests.slice(0, 2).map((request) => {
					const data = request.arguments?.data as { method?: string };
					return data.method;
				}),
			).toEqual(["update", "custom"]);

			await runtime.dispose();
		} finally {
			vi.useRealTimers();
		}
	});

	test("runs a dynamic initializer command inside the active protocol transaction", async () => {
		const rootId = "root-model";
		const childId = "dynamic-child";
		const initialized: unknown[] = [];
		const callServerTool = vi.fn(
			async (request: {
				name: string;
				arguments?: Record<string, unknown>;
			}): Promise<CallToolResult> => {
				if (request.name === "anywidget_dispose") return { content: [] };
				const data = request.arguments?.data as
					| { method?: string; content?: Record<string, unknown> }
					| undefined;
				if (data?.method === "custom") {
					return {
						content: [],
						_meta: {
							anywidget: {
								messages: [
									{
										modelId: childId,
										data: {
											method: "custom",
											content: {
												id: data.content?.id,
												kind: "anywidget-command-response",
												response: { ready: true },
											},
										},
										buffers: [],
									},
								],
							},
						},
					};
				}
				return {
					content: [],
					_meta: {
						anywidget: {
							models: {
								[childId]: {
									modelId: childId,
									state: { _esm: "dynamic" },
								},
							},
							messages: [],
						},
					},
				};
			},
		);
		const runtime = new WidgetRuntime(
			{
				instanceId: "instance-1",
				rootModelId: rootId,
				models: {
					[rootId]: {
						modelId: rootId,
						state: { _esm: "root" },
					},
				},
			},
			new ToolCallQueue({ callServerTool } as unknown as App),
			{
				getHostCapabilities: () => ({}),
				updateModelContext: vi.fn().mockResolvedValue({}),
			} as unknown as App,
			Promise.resolve(),
			(bindingRuntime, model) => {
				if (model.modelId === rootId) return new FakeBinding(model, []);
				return new WidgetBinding(bindingRuntime, model, {
					reportError: vi.fn(),
					replaceCss: async () => undefined,
					loadWidget: async () => ({
						async initialize({ experimental }) {
							const [result] = await experimental.invoke("initialize_child");
							initialized.push(result);
						},
					}),
				});
			},
		);

		await expect(
			runtime.send(rootId, { method: "update", state: {} }, [], "outer-operation"),
		).resolves.toBeUndefined();

		expect(initialized).toEqual([{ ready: true }]);
		const comms = callServerTool.mock.calls
			.map(([request]) => request)
			.filter((request) => request.name === "anywidget_comm");
		expect(comms).toHaveLength(2);
		expect(
			comms.map((request) => {
				const data = request.arguments?.data as { method?: string };
				return data.method;
			}),
		).toEqual(["update", "custom"]);

		await runtime.dispose();
	});

	test("preserves outer, queued, and initializer command order with authoritative nested state", async () => {
		const rootId = "root-model";
		const childId = "dynamic-child";
		const initializeStarted = deferred<void>();
		const continueInitialize = deferred<void>();
		const order: string[] = [];
		const outerCustom: unknown[] = [];
		const callServerTool = vi.fn(
			async (request: {
				name: string;
				arguments?: Record<string, unknown>;
			}): Promise<CallToolResult> => {
				if (request.name === "anywidget_dispose") return { content: [] };
				const data = request.arguments?.data as
					| { method?: string; content?: Record<string, unknown> }
					| undefined;
				if (request.arguments?.operation_id === "outer-operation") {
					order.push("O");
					return {
						content: [],
						_meta: {
							anywidget: {
								models: {
									[childId]: {
										modelId: childId,
										state: { _esm: "dynamic", value: 0 },
									},
								},
								messages: [
									{
										modelId: childId,
										data: { method: "custom", content: { source: "outer" } },
										buffers: [],
									},
									{
										modelId: childId,
										data: { method: "update", state: { value: 1 } },
										buffers: [],
									},
								],
							},
						},
					};
				}
				if (data?.method === "update") {
					order.push("B");
					return { content: [] };
				}
				order.push("C");
				return {
					content: [],
					_meta: {
						anywidget: {
							messages: [
								{
									modelId: childId,
									data: {
										method: "custom",
										content: {
											id: data?.content?.id,
											kind: "anywidget-command-response",
											response: { ready: true },
										},
									},
									buffers: [],
								},
								{
									modelId: childId,
									data: { method: "update", state: { value: 2 } },
									buffers: [],
								},
							],
						},
					},
				};
			},
		);
		const runtime = new WidgetRuntime(
			{
				instanceId: "instance-1",
				rootModelId: rootId,
				models: {
					[rootId]: { modelId: rootId, state: { _esm: "root", value: 0 } },
				},
			},
			new ToolCallQueue({ callServerTool } as unknown as App),
			{
				getHostCapabilities: () => ({}),
				updateModelContext: vi.fn().mockResolvedValue({}),
			} as unknown as App,
			Promise.resolve(),
			(bindingRuntime, model) => {
				if (model.modelId === rootId) return new FakeBinding(model, []);
				return new WidgetBinding(bindingRuntime, model, {
					reportError: vi.fn(),
					replaceCss: async () => undefined,
					loadWidget: async () => ({
						async initialize({ model: childModel, experimental }) {
							initializeStarted.resolve(undefined);
							await continueInitialize.promise;
							await experimental.invoke("initialize_child");
							childModel.on("msg:custom", (content) => {
								if (typeof content === "object" && content !== null && "source" in content)
									outerCustom.push(content);
							});
						},
					}),
				});
			},
		);

		const outer = runtime.send(rootId, { method: "update", state: {} }, [], "outer-operation");
		await initializeStarted.promise;
		const rootModel = runtime.model(rootId);
		rootModel.set("value", 1);
		rootModel.save_changes();
		continueInitialize.resolve(undefined);
		await outer;

		expect(order).toEqual(["O", "B", "C"]);
		expect(outerCustom).toEqual([{ source: "outer" }]);
		expect(runtime.model(childId).get("value")).toBe(2);
		await runtime.dispose();
	});

	test("serializes Promise.all commands and multiple dynamic initializers", async () => {
		const rootId = "root-model";
		const childIds = ["child-a", "child-b"];
		const commands: string[] = [];
		let activeCalls = 0;
		let maxActiveCalls = 0;
		const callServerTool = vi.fn(
			async (request: {
				name: string;
				arguments?: Record<string, unknown>;
			}): Promise<CallToolResult> => {
				if (request.name === "anywidget_dispose") return { content: [] };
				const data = request.arguments?.data as
					| { method?: string; content?: Record<string, unknown> }
					| undefined;
				if (request.arguments?.operation_id === "outer-operation") {
					return {
						content: [],
						_meta: {
							anywidget: {
								models: Object.fromEntries(
									childIds.map((modelId) => [modelId, { modelId, state: { _esm: modelId } }]),
								),
								messages: [],
							},
						},
					};
				}
				const command = String(data?.content?.name);
				commands.push(command);
				activeCalls += 1;
				maxActiveCalls = Math.max(maxActiveCalls, activeCalls);
				await Promise.resolve();
				activeCalls -= 1;
				return {
					content: [],
					_meta: {
						anywidget: {
							messages: [
								{
									modelId: request.arguments?.model_id,
									data: {
										method: "custom",
										content: {
											id: data?.content?.id,
											kind: "anywidget-command-response",
											response: command,
										},
									},
									buffers: [],
								},
							],
						},
					},
				};
			},
		);
		const runtime = new WidgetRuntime(
			{
				instanceId: "instance-1",
				rootModelId: rootId,
				models: { [rootId]: { modelId: rootId, state: { _esm: "root" } } },
			},
			new ToolCallQueue({ callServerTool } as unknown as App),
			{
				getHostCapabilities: () => ({}),
				updateModelContext: vi.fn().mockResolvedValue({}),
			} as unknown as App,
			Promise.resolve(),
			(bindingRuntime, model) => {
				if (model.modelId === rootId) return new FakeBinding(model, []);
				return new WidgetBinding(bindingRuntime, model, {
					reportError: vi.fn(),
					replaceCss: async () => undefined,
					loadWidget: async () => ({
						async initialize({ experimental }) {
							await Promise.all([
								experimental.invoke(`${model.modelId}:1`),
								experimental.invoke(`${model.modelId}:2`),
							]);
						},
					}),
				});
			},
		);

		await runtime.send(rootId, { method: "update", state: {} }, [], "outer-operation");

		expect(commands).toEqual(["child-a:1", "child-a:2", "child-b:1", "child-b:2"]);
		expect(maxActiveCalls).toBe(1);
		await runtime.dispose();
	});

	test("routes a retained initializer API through the central scheduler", async () => {
		const rootId = "root-model";
		const childId = "dynamic-child";
		const blocked = deferred<CallToolResult>();
		const order: string[] = [];
		const callServerTool = vi.fn(
			async (request: {
				name: string;
				arguments?: Record<string, unknown>;
			}): Promise<CallToolResult> => {
				if (request.name === "anywidget_dispose") return { content: [] };
				const data = request.arguments?.data as
					| { method?: string; content?: Record<string, unknown> }
					| undefined;
				if (request.arguments?.operation_id === "outer-operation") {
					order.push("outer");
					return {
						content: [],
						_meta: {
							anywidget: {
								models: {
									[childId]: { modelId: childId, state: { _esm: "dynamic" } },
								},
								messages: [],
							},
						},
					};
				}
				if (data?.method === "update") {
					order.push("blocked-update");
					return blocked.promise;
				}
				order.push("retained-command");
				return {
					content: [],
					_meta: {
						anywidget: {
							messages: [
								{
									modelId: childId,
									data: {
										method: "custom",
										content: {
											id: data?.content?.id,
											kind: "anywidget-command-response",
											response: "retained",
										},
									},
									buffers: [],
								},
							],
						},
					},
				};
			},
		);
		const runtime = new WidgetRuntime(
			{
				instanceId: "instance-1",
				rootModelId: rootId,
				models: { [rootId]: { modelId: rootId, state: { _esm: "root", value: 0 } } },
			},
			new ToolCallQueue({ callServerTool } as unknown as App),
			{
				getHostCapabilities: () => ({}),
				updateModelContext: vi.fn().mockResolvedValue({}),
			} as unknown as App,
			Promise.resolve(),
			(bindingRuntime, model) => {
				if (model.modelId === rootId) return new FakeBinding(model, []);
				return new WidgetBinding(bindingRuntime, model, {
					reportError: vi.fn(),
					replaceCss: async () => undefined,
					loadWidget: async () => ({
						initialize({ experimental }) {
							return { invoke: () => experimental.invoke("retained") };
						},
					}),
				});
			},
		);
		await runtime.send(rootId, { method: "update", state: {} }, [], "outer-operation");

		const rootModel = runtime.model(rootId);
		rootModel.set("value", 1);
		rootModel.save_changes();
		await vi.waitFor(() => expect(order).toEqual(["outer", "blocked-update"]));
		const exports = (await runtime.binding(childId).getExports()) as {
			invoke(): Promise<[unknown, DataView[]]>;
		};
		const retained = exports.invoke();
		await Promise.resolve();
		expect(order).toEqual(["outer", "blocked-update"]);

		blocked.resolve({ content: [] });
		await expect(retained).resolves.toEqual(["retained", []]);
		expect(order).toEqual(["outer", "blocked-update", "retained-command"]);
		await runtime.dispose();
	});

	test("settles a command after slow result removal completes", async () => {
		const removal = deferred<void>();
		let commandId: unknown;
		const callServerTool = vi.fn(
			async (request: {
				name: string;
				arguments?: Record<string, unknown>;
			}): Promise<CallToolResult> => {
				if (request.name === "anywidget_dispose") return { content: [] };
				const data = request.arguments?.data as { content?: Record<string, unknown> };
				commandId = data.content?.id;
				return {
					content: [],
					_meta: {
						anywidget: {
							messages: [
								{
									modelId: "root-model",
									data: {
										method: "custom",
										content: {
											id: commandId,
											kind: "anywidget-command-response",
											response: "done",
										},
									},
									buffers: [],
								},
							],
							removedModelIds: ["slow-model"],
						},
					},
				};
			},
		);
		const runtime = new WidgetRuntime(
			{
				instanceId: "instance-1",
				rootModelId: "root-model",
				models: {
					"root-model": { modelId: "root-model", state: { _esm: "root" } },
					"slow-model": { modelId: "slow-model", state: { _esm: "slow" } },
				},
			},
			new ToolCallQueue({ callServerTool } as unknown as App),
			{
				getHostCapabilities: () => ({}),
				updateModelContext: vi.fn().mockResolvedValue({}),
			} as unknown as App,
			Promise.resolve(),
			(_bindingRuntime, model): RuntimeBinding => ({
				async initialize() {},
				async render() {},
				async getExports() {},
				async dispose() {
					if (model.modelId === "slow-model") await removal.promise;
				},
			}),
		);
		const experimental = runtime.experimental(
			runtime.model("root-model"),
			new AbortController().signal,
		);

		const invoked = experimental.invoke("remove");
		let settled = false;
		void invoked.then(() => {
			settled = true;
		});
		await vi.waitFor(() => expect(commandId).toBeDefined());
		await Promise.resolve();
		expect(settled).toBe(false);

		removal.resolve(undefined);
		await expect(invoked).resolves.toEqual(["done", []]);
		await runtime.dispose();
	});

	test("rejects a command when result application fails after its response", async () => {
		const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
		const callServerTool = vi.fn(
			async (request: {
				name: string;
				arguments?: Record<string, unknown>;
			}): Promise<CallToolResult> => {
				if (request.name === "anywidget_dispose") return { content: [] };
				const data = request.arguments?.data as { content?: Record<string, unknown> };
				return {
					content: [],
					_meta: {
						anywidget: {
							messages: [
								{
									modelId: "root-model",
									data: {
										method: "custom",
										content: {
											id: data.content?.id,
											kind: "anywidget-command-response",
											response: "premature",
										},
									},
									buffers: [],
								},
							],
							removedModelIds: ["root-model"],
						},
					},
				};
			},
		);
		const runtime = new WidgetRuntime(
			{
				instanceId: "instance-1",
				rootModelId: "root-model",
				models: { "root-model": { modelId: "root-model", state: { _esm: "root" } } },
			},
			new ToolCallQueue({ callServerTool } as unknown as App),
			{
				getHostCapabilities: () => ({}),
				updateModelContext: vi.fn().mockResolvedValue({}),
			} as unknown as App,
			Promise.resolve(),
			(_runtime, model) => new FakeBinding(model, []),
		);
		const experimental = runtime.experimental(
			runtime.model("root-model"),
			new AbortController().signal,
		);

		await expect(experimental.invoke("fail-after-response")).rejects.toThrow(
			"root anywidget model cannot be removed",
		);
		await runtime.dispose();
		consoleError.mockRestore();
	});

	test("cancels a stalled initializer command and releases the global queue", async () => {
		vi.useFakeTimers();
		const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
		try {
			const callsByName: string[] = [];
			const callServerTool = vi.fn(
				async (request: {
					name: string;
					arguments?: Record<string, unknown>;
				}): Promise<CallToolResult> => {
					callsByName.push(request.name);
					if (request.name === "anywidget_dispose" || request.name === "after") {
						return { content: [] };
					}
					if (request.arguments?.operation_id !== "outer-operation") {
						return new Promise<never>(() => undefined);
					}
					return {
						content: [],
						_meta: {
							anywidget: {
								models: {
									"dynamic-child": {
										modelId: "dynamic-child",
										state: { _esm: "dynamic" },
									},
								},
								messages: [],
							},
						},
					};
				},
			);
			const calls = new ToolCallQueue({ callServerTool } as unknown as App);
			const runtime = new WidgetRuntime(
				{
					instanceId: "instance-1",
					rootModelId: "root-model",
					models: { "root-model": { modelId: "root-model", state: { _esm: "root" } } },
				},
				calls,
				{
					getHostCapabilities: () => ({}),
					updateModelContext: vi.fn().mockResolvedValue({}),
				} as unknown as App,
				Promise.resolve(),
				(bindingRuntime, model) => {
					if (model.modelId === "root-model") return new FakeBinding(model, []);
					return new WidgetBinding(bindingRuntime, model, {
						reportError: vi.fn(),
						replaceCss: async () => undefined,
						loadWidget: async () => ({
							async initialize({ experimental }) {
								await experimental.invoke("never");
							},
						}),
						timeoutMilliseconds: 25,
					});
				},
			);

			const outer = runtime.send(
				"root-model",
				{ method: "update", state: {} },
				[],
				"outer-operation",
			);
			const outerFailure = expect(outer).rejects.toBeDefined();
			await vi.advanceTimersByTimeAsync(0);
			const after = calls.call("after", {});
			await vi.advanceTimersByTimeAsync(25);

			await outerFailure;
			await expect(after).resolves.toEqual({ content: [] });
			expect(callsByName).toContain("anywidget_dispose");
			await runtime.dispose();
		} finally {
			consoleError.mockRestore();
			vi.useRealTimers();
		}
	});

	test("blocks experimental commands and child renders after scope abort", async () => {
		const callServerTool = vi.fn().mockResolvedValue({ content: [] });
		const render = vi.fn().mockResolvedValue(undefined);
		const runtime = new WidgetRuntime(
			{
				instanceId: "instance-1",
				rootModelId: "root-model",
				models: {
					"root-model": { modelId: "root-model", state: { _esm: "root" } },
					"child-model": { modelId: "child-model", state: { _esm: "child" } },
				},
			},
			new ToolCallQueue({ callServerTool } as unknown as App),
			{
				getHostCapabilities: () => ({}),
				updateModelContext: vi.fn().mockResolvedValue({}),
			} as unknown as App,
			Promise.resolve(),
			(_bindingRuntime, model): RuntimeBinding => ({
				async initialize() {},
				render,
				async getExports() {
					return { modelId: model.modelId };
				},
				async dispose() {},
			}),
		);
		await Promise.all([
			runtime.binding("root-model").initialize(),
			runtime.binding("child-model").initialize(),
		]);

		const commandScope = new AbortController();
		const experimental = runtime.experimental(runtime.model("root-model"), commandScope.signal);
		commandScope.abort();
		await expect(experimental.invoke("stale-command")).rejects.toMatchObject({
			name: "AbortError",
		});

		const renderScope = new AbortController();
		const host = runtime.host(renderScope.signal);
		const child = await host.getWidget("anywidget:child-model");
		renderScope.abort();
		await expect(child.render({ el: {} as HTMLElement })).rejects.toMatchObject({
			name: "AbortError",
		});
		await expect(host.getWidget("anywidget:child-model")).rejects.toMatchObject({
			name: "AbortError",
		});

		expect(render).not.toHaveBeenCalled();
		expect(
			callServerTool.mock.calls.filter(([request]) => request.name === "anywidget_comm"),
		).toHaveLength(0);
		await runtime.dispose();
	});

	test("joins concurrent disposal through one teardown task", async () => {
		const cleanup = deferred<void>();
		const call = vi.fn().mockResolvedValue({ content: [] });
		const callNow = vi.fn().mockResolvedValue({ content: [] });
		const runtime = new WidgetRuntime(
			{
				instanceId: "instance-1",
				rootModelId: "root-model",
				models: {
					"root-model": { modelId: "root-model", state: { _esm: "root" } },
				},
			},
			fakeQueue(call, callNow),
			{
				getHostCapabilities: () => ({}),
				updateModelContext: vi.fn().mockResolvedValue({}),
			} as unknown as App,
			Promise.resolve(),
			(): RuntimeBinding => ({
				async initialize() {},
				async render() {},
				async getExports() {},
				async dispose() {
					await cleanup.promise;
				},
			}),
		);

		const first = runtime.dispose();
		const second = runtime.dispose();
		expect(second).toBe(first);
		let complete = false;
		void second.then(() => {
			complete = true;
		});
		await Promise.resolve();
		expect(complete).toBe(false);

		cleanup.resolve(undefined);
		await first;
		expect(complete).toBe(true);
		expect(callNow).toHaveBeenCalledTimes(1);
	});

	test("rolls back every added model when one initialization fails", async () => {
		const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
		const rootId = "root-model";
		const firstId = "first-added";
		const failedId = "failed-added";
		const disposed: string[] = [];
		const update: CallToolResult = {
			content: [],
			_meta: {
				anywidget: {
					models: {
						[firstId]: {
							modelId: firstId,
							state: { _esm: "export default {}" },
						},
						[failedId]: {
							modelId: failedId,
							state: { _esm: "export default {}" },
						},
					},
					messages: [],
				},
			},
		};
		const call = vi.fn().mockResolvedValue(update);
		const callNow = vi.fn().mockResolvedValue({ content: [] });
		const runtime = new WidgetRuntime(
			{
				instanceId: "instance-1",
				rootModelId: rootId,
				models: {
					[rootId]: {
						modelId: rootId,
						state: { _esm: "export default {}" },
					},
				},
			},
			fakeQueue(call, callNow),
			{
				getHostCapabilities: () => ({}),
				updateModelContext: vi.fn().mockResolvedValue({}),
			} as unknown as App,
			Promise.resolve(),
			(_runtime, model): RuntimeBinding => ({
				async initialize() {
					if (model.modelId === failedId) throw new Error("initialize failed");
				},
				async render() {},
				async getExports() {},
				async dispose() {
					disposed.push(model.modelId);
				},
			}),
		);

		await expect(
			runtime.send(rootId, { method: "update", state: {} }, [], "operation-1"),
		).rejects.toThrow("initialize failed");

		expect(() => runtime.model(firstId)).toThrow("Unknown anywidget model");
		expect(() => runtime.model(failedId)).toThrow("Unknown anywidget model");
		expect(disposed).toEqual(expect.arrayContaining([firstId, failedId]));
		expect(callNow).toHaveBeenCalledWith(
			"anywidget_dispose",
			{ instance_id: "instance-1" },
			expect.any(AbortSignal),
		);
		expect(consoleError).toHaveBeenCalledWith(
			expect.objectContaining({ message: "initialize failed" }),
		);

		await runtime.dispose();
		consoleError.mockRestore();
	});
});
