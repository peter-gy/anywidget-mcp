// @vitest-environment jsdom

import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { describe, expect, test, vi } from "vite-plus/test";

import { WidgetBinding, type RuntimeBinding, type WidgetDefinition } from "../src/binding";
import { WidgetRuntime } from "../src/runtime";
import type { RuntimeValue } from "../src/runtime-value";
import { ToolCallQueue, type ToolArguments, type ToolRequest } from "../src/tool-calls";
import {
	deferred,
	fakeQueue,
	FakeBinding,
	fixtureRecord,
	fixtureString,
	fixtureStrings,
	fixtureTextValue,
} from "./runtime-test-support";

describe("WidgetRuntime launch and model graph", () => {
	test("exposes a child's initialized class instance to its parent", async () => {
		class CounterApi {
			value() {
				return 42;
			}
		}
		const api = new CounterApi();
		const element = document.createElement("div");
		const runtime = new WidgetRuntime(
			{
				instanceId: "composition",
				rootModelId: "parent",
				models: {
					parent: { state: { _esm: "parent", child: "anywidget:child" } },
					child: { state: { _esm: "child" } },
				},
			},
			fakeQueue(
				async () => ({ content: [] }),
				async () => ({ content: [] }),
			),
			{ getHostCapabilities: () => ({}), updateModelContext: vi.fn() },
			Promise.resolve(),
			(owner, model) =>
				new WidgetBinding(owner, model, {
					reportError: vi.fn(),
					replaceCss: async () => undefined,
					loadWidget: async () =>
						model.modelId === "child"
							? { initialize: () => api }
							: {
									async render({ host, el }) {
										const child = await host.getWidget<CounterApi>("anywidget:child");
										expect(child.exports).toBe(api);
										el.textContent = String(child.exports.value());
									},
								},
				}),
		);
		try {
			await runtime.mount(element);
			expect(element.textContent).toBe("42");
		} finally {
			await runtime.dispose();
		}
	});

	test.each(["abort", "timeout"])("rejects a stalled child lookup on %s", async (ending) => {
		vi.useFakeTimers();
		const events: string[] = [];
		const runtime = new WidgetRuntime(
			{
				instanceId: "composition",
				rootModelId: "child",
				models: { child: { state: { _esm: "child" } } },
			},
			fakeQueue(
				async () => ({ content: [] }),
				async () => ({ content: [] }),
			),
			{ getHostCapabilities: () => ({}), updateModelContext: vi.fn() },
			Promise.resolve(),
			(_owner, model) => new FakeBinding(model, events),
		);
		const controller = new AbortController();
		const result = runtime.host(controller.signal).getWidget("anywidget:child");
		const rejected = expect(result).rejects.toThrow(
			ending === "abort"
				? "Parent view closed"
				: "Timed out waiting for widget anywidget:child to initialize",
		);
		try {
			if (ending === "abort") controller.abort(new Error("Parent view closed"));
			else await vi.advanceTimersByTimeAsync(10_000);
			await rejected;
		} finally {
			await runtime.dispose();
			vi.useRealTimers();
		}
	});

	test("publishes the latest nested context after the complete graph transaction", async () => {
		vi.useFakeTimers();
		const initializing = deferred<void>();
		const runNested = deferred<void>();
		const nestedComplete = deferred<void>();
		const finishInitialize = deferred<void>();
		const updateModelContext = vi.fn().mockResolvedValue({});
		const calls = fakeQueue(
			async (_name, args) => ({
				content: [],
				_meta: {
					anywidget:
						args.operation_id === 1
							? {
									models: { child: { state: { _esm: "export default {}" } } },
									context: { version: 2, tool: "probe", state: { value: 1 } },
								}
							: { context: { version: 3, tool: "probe", state: { value: 2 } } },
				},
			}),
			async () => ({ content: [] }),
		);
		const runtime = new WidgetRuntime(
			{
				instanceId: "instance-1",
				rootModelId: "root",
				models: { root: { state: { _esm: "export default {}" } } },
			},
			calls,
			{
				getHostCapabilities: () => ({ updateModelContext: { structuredContent: {} } }),
				updateModelContext,
			},
			Promise.resolve(),
			(owner, model) => ({
				async initialize() {
					if (model.modelId !== "child") return;
					initializing.resolve(undefined);
					await runNested.promise;
					await owner.send("child", { method: "custom" }, []);
					nestedComplete.resolve(undefined);
					await finishInitialize.promise;
				},
				async render() {},
				async getExports() {
					return {};
				},
				async dispose() {},
			}),
		);
		const transaction = runtime.send("root", { method: "custom" }, []);
		try {
			await initializing.promise;
			await vi.runOnlyPendingTimersAsync();
			expect(updateModelContext).not.toHaveBeenCalled();
			runNested.resolve(undefined);
			await nestedComplete.promise;
			await vi.runOnlyPendingTimersAsync();
			expect(updateModelContext).not.toHaveBeenCalled();
			finishInitialize.resolve(undefined);
			await transaction;
			await vi.runOnlyPendingTimersAsync();

			expect(updateModelContext).toHaveBeenCalledExactlyOnceWith(
				{ structuredContent: { tool: "probe", state: { value: 2 } } },
				{ signal: expect.any(AbortSignal) },
			);
		} finally {
			runNested.resolve(undefined);
			finishInitialize.resolve(undefined);
			await transaction.catch(() => undefined);
			await runtime.dispose();
			vi.useRealTimers();
		}
	});

	test("aborts a stalled mount before render and disposes its server session once", async () => {
		const initializing = deferred<void>();
		const release = deferred<void>();
		const render = vi.fn();
		const disposeBinding = vi.fn();
		const call = vi.fn().mockResolvedValue({ content: [] });
		const callNow = vi.fn().mockResolvedValue({ content: [] });
		const runtime = new WidgetRuntime(
			{
				instanceId: "mount-session",
				rootModelId: "root-model",
				models: {
					"root-model": { modelId: "root-model", state: { _esm: "root" } },
				},
			},
			fakeQueue(call, callNow),
			{
				getHostCapabilities: () => ({}),
				updateModelContext: vi.fn().mockResolvedValue({}),
			},
			Promise.resolve(),
			(): RuntimeBinding => ({
				async initialize() {
					initializing.resolve(undefined);
					await release.promise;
				},
				async render() {
					render();
				},
				async getExports() {},
				async dispose() {
					disposeBinding();
				},
			}),
		);
		const controller = new AbortController();
		const mounting = runtime.mount(document.createElement("div"), controller.signal);
		await initializing.promise;

		controller.abort(new DOMException("superseded", "AbortError"));

		await expect(mounting).rejects.toMatchObject({ name: "AbortError" });
		await runtime.dispose(controller.signal.reason);
		release.resolve(undefined);
		await Promise.resolve();
		expect(render).not.toHaveBeenCalled();
		expect(disposeBinding).toHaveBeenCalledOnce();
		expect(callNow).toHaveBeenCalledOnce();
		expect(callNow).toHaveBeenCalledWith(
			"anywidget_dispose",
			{ session_id: "mount-session" },
			expect.any(AbortSignal),
		);
	});

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
			};
			const loadWidget = vi.fn(async (source: string): Promise<WidgetDefinition> => {
				events.push(`load:${source}`);
				return {
					initialize({ model }) {
						phase = "initialize";
						events.push(
							`initialize:${fixtureString(model.get("_esm"))}:${fixtureTextValue(model.get("value"))}`,
						);
						model.on("msg:custom", (content) => {
							custom.push({ content, phase });
							events.push(`custom:${phase}`);
						});
						phase = "initialized";
					},
					render({ model }) {
						phase = "render";
						events.push(
							`render:${fixtureString(model.get("_esm"))}:${fixtureTextValue(model.get("value"))}`,
						);
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
			const element = document.createElement("div");

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
		};

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
			async (name: string, _args: ToolArguments, _signal?: AbortSignal): Promise<CallToolResult> =>
				name === "anywidget_comm" ? update : { content: [] },
		);
		const callNow = vi.fn().mockResolvedValue({ content: [] });
		const calls = fakeQueue(call, callNow);
		const app = {
			getHostCapabilities: () => ({}),
			updateModelContext: vi.fn().mockResolvedValue({}),
		};
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
			const ref = fixtureString(root.get("child"));
			resolvedWidget = runtime
				.host(new AbortController().signal)
				.getWidget(ref)
				.then(() => {
					events.push("resolved:new-child");
				});
		});

		await runtime.send(rootId, { method: "update", state: {} }, []);
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
				operation_id: 1,
				acknowledged_operation_id: 0,
			},
			expect.any(AbortSignal),
		);

		await runtime.dispose();
		expect(callNow).toHaveBeenCalledWith(
			"anywidget_dispose",
			{ session_id: "instance-1" },
			expect.any(AbortSignal),
		);
	});

	test("acknowledges transient removals from a poll executed after browser cleanup", async () => {
		vi.useFakeTimers();
		try {
			const rootId = "root-model";
			const transientId = "transient-child";
			const outerResult = deferred<CallToolResult>();
			const cleanup = vi.fn();
			const pollAcknowledgments: RuntimeValue[] = [];
			const callServerTool = vi.fn(async (request: ToolRequest): Promise<CallToolResult> => {
				if (request.name === "anywidget_dispose") return { content: [] };
				if (request.name === "anywidget_poll") {
					pollAcknowledgments.push(fixtureStrings(request.arguments?.acknowledged_model_ids));
					return { content: [] };
				}
				if (request.arguments?.operation_id === 1) {
					return outerResult.promise;
				}
				const data = fixtureRecord(request.arguments?.data);
				if (data.method === "custom") {
					const content = fixtureRecord(data.content);
					return {
						content: [],
						_meta: {
							anywidget: {
								messages: [
									{
										modelId: transientId,
										data: {
											method: "custom",
											content: {
												id: content.id,
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
				return { content: [] };
			});
			const runtime = new WidgetRuntime(
				{
					instanceId: "instance-1",
					rootModelId: rootId,
					models: {
						[rootId]: { modelId: rootId, state: { _esm: "root" } },
					},
				},
				new ToolCallQueue({ callServerTool }),
				{
					getHostCapabilities: () => ({}),
					updateModelContext: vi.fn().mockResolvedValue({}),
				},
				Promise.resolve(),
				(bindingRuntime, model) => {
					if (model.modelId === rootId) return new FakeBinding(model, []);
					return new WidgetBinding(bindingRuntime, model, {
						reportError: vi.fn(),
						replaceCss: async () => undefined,
						loadWidget: async () => ({
							async initialize({ experimental }) {
								await experimental.invoke("initialize_child");
								return cleanup;
							},
						}),
					});
				},
			);
			await runtime.mount(document.createElement("div"));
			const outer = runtime.send(rootId, { method: "update", state: {} }, []);

			await vi.advanceTimersByTimeAsync(500);
			expect(pollAcknowledgments).toEqual([]);
			outerResult.resolve({
				content: [],
				_meta: {
					anywidget: {
						models: {
							[transientId]: {
								modelId: transientId,
								state: { _esm: "transient" },
							},
						},
						removedModelIds: [transientId],
						messages: [],
					},
				},
			});

			await outer;
			expect(cleanup).toHaveBeenCalledOnce();
			expect(() => runtime.model(transientId)).toThrow("Unknown anywidget model");
			expect(pollAcknowledgments).toEqual([[]]);

			await vi.advanceTimersByTimeAsync(500);
			expect(pollAcknowledgments).toEqual([[], [transientId]]);
			await vi.advanceTimersByTimeAsync(1000);
			expect(pollAcknowledgments).toEqual([[], [transientId], []]);

			await runtime.dispose();
		} finally {
			vi.useRealTimers();
		}
	});

	test("disposes the runtime instead of acknowledging incomplete model cleanup", async () => {
		vi.useFakeTimers();
		const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
		try {
			const call = vi.fn(
				async (
					name: string,
					_args: ToolArguments,
					_signal?: AbortSignal,
				): Promise<CallToolResult> =>
					name === "anywidget_comm"
						? {
								content: [],
								_meta: { anywidget: { removedModelIds: ["child-model"] } },
							}
						: { content: [] },
			);
			const callNow = vi.fn().mockResolvedValue({ content: [] });
			const runtime = new WidgetRuntime(
				{
					instanceId: "instance-1",
					rootModelId: "root-model",
					models: {
						"root-model": { modelId: "root-model", state: { _esm: "root" } },
						"child-model": { modelId: "child-model", state: { _esm: "child" } },
					},
				},
				fakeQueue(call, callNow),
				{
					getHostCapabilities: () => ({}),
					updateModelContext: vi.fn().mockResolvedValue({}),
				},
				Promise.resolve(),
				(_bindingRuntime, model): RuntimeBinding => ({
					async initialize() {},
					async render() {},
					async getExports() {},
					async dispose() {
						if (model.modelId === "child-model") await new Promise<never>(() => undefined);
					},
				}),
			);

			const sending = runtime.send("root-model", { method: "update", state: {} }, []);
			const rejected = expect(sending).rejects.toThrow(
				"Timed out while disposing anywidget binding",
			);
			await vi.advanceTimersByTimeAsync(3000);

			await rejected;
			await runtime.dispose();
			expect(callNow).toHaveBeenCalledOnce();
			expect(callNow).toHaveBeenCalledWith(
				"anywidget_dispose",
				{ session_id: "instance-1" },
				expect.any(AbortSignal),
			);
			expect(
				call.mock.calls.some(
					([name, args]) =>
						name === "anywidget_poll" &&
						Array.isArray(args.acknowledged_model_ids) &&
						args.acknowledged_model_ids.includes("child-model"),
				),
			).toBe(false);
		} finally {
			consoleError.mockRestore();
			vi.useRealTimers();
		}
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
			},
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

		await expect(runtime.send(rootId, { method: "update", state: {} }, [])).rejects.toThrow(
			"initialize failed",
		);

		expect(() => runtime.model(firstId)).toThrow("Unknown anywidget model");
		expect(() => runtime.model(failedId)).toThrow("Unknown anywidget model");
		expect(disposed).toEqual(expect.arrayContaining([firstId, failedId]));
		expect(callNow).toHaveBeenCalledWith(
			"anywidget_dispose",
			{ session_id: "instance-1" },
			expect.any(AbortSignal),
		);
		expect(consoleError).toHaveBeenCalledWith(
			expect.objectContaining({ message: expect.stringContaining("initialize failed") }),
		);

		await runtime.dispose();
		consoleError.mockRestore();
	});
});
