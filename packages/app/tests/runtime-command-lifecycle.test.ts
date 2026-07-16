import type { App } from "@modelcontextprotocol/ext-apps";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { describe, expect, test, vi } from "vite-plus/test";

import { WidgetRuntime } from "../src/app";
import { WidgetBinding, type RuntimeBinding } from "../src/binding";
import { ToolCallQueue } from "../src/tool-calls";
import { deferred, fakeQueue, FakeBinding } from "./runtime-test-support";

describe("WidgetRuntime command lifecycle", () => {
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

	test("removes a queued command when its request signal aborts", async () => {
		const blocked = deferred<CallToolResult>();
		const commands: string[] = [];
		const callServerTool = vi.fn(
			async (request: {
				name: string;
				arguments?: Record<string, unknown>;
			}): Promise<CallToolResult> => {
				if (request.name === "anywidget_dispose" || request.name === "after") {
					return { content: [] };
				}
				const data = request.arguments?.data as
					| { method?: string; content?: Record<string, unknown> }
					| undefined;
				if (data?.method === "update") return blocked.promise;
				commands.push(String(data?.content?.name));
				return { content: [] };
			},
		);
		const calls = new ToolCallQueue({ callServerTool } as unknown as App);
		const runtime = new WidgetRuntime(
			{
				instanceId: "instance-1",
				rootModelId: "root-model",
				models: {
					"root-model": {
						modelId: "root-model",
						state: { _esm: "root", value: 0 },
					},
				},
			},
			calls,
			{
				getHostCapabilities: () => ({}),
				updateModelContext: vi.fn().mockResolvedValue({}),
			} as unknown as App,
			Promise.resolve(),
			(_bindingRuntime, model) => new FakeBinding(model, []),
		);

		const model = runtime.model("root-model");
		model.set("value", 1);
		model.save_changes();
		await vi.waitFor(() => expect(callServerTool).toHaveBeenCalledTimes(1));

		const request = new AbortController();
		const experimental = runtime.experimental(model, new AbortController().signal);
		const invoked = experimental.invoke("stale", undefined, { signal: request.signal });
		request.abort(new DOMException("request expired", "AbortError"));
		await expect(invoked).rejects.toMatchObject({ name: "AbortError" });

		blocked.resolve({ content: [] });
		await expect(calls.call("after", {})).resolves.toEqual({ content: [] });
		expect(commands).toEqual([]);
		await runtime.dispose();
	});

	test("does not dispatch a command aborted during queue handoff", async () => {
		const callServerTool = vi.fn().mockResolvedValue({ content: [] });
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
			(_bindingRuntime, model) => new FakeBinding(model, []),
		);
		const request = new AbortController();
		const invoked = runtime
			.experimental(runtime.model("root-model"), new AbortController().signal)
			.invoke("handoff", undefined, { signal: request.signal });

		request.abort(new DOMException("request expired", "AbortError"));

		await expect(invoked).rejects.toMatchObject({ name: "AbortError" });
		await expect(calls.call("after", {})).resolves.toEqual({ content: [] });
		expect(
			callServerTool.mock.calls.filter(([request]) => request.name === "anywidget_comm"),
		).toHaveLength(0);
		await runtime.dispose();
	});

	test("applies an active command response after its caller aborts", async () => {
		const response = deferred<CallToolResult>();
		const callServerTool = vi.fn(
			async (request: {
				name: string;
				arguments?: Record<string, unknown>;
			}): Promise<CallToolResult> => {
				if (request.name === "anywidget_dispose") return { content: [] };
				return response.promise;
			},
		);
		const runtime = new WidgetRuntime(
			{
				instanceId: "instance-1",
				rootModelId: "root-model",
				models: {
					"root-model": {
						modelId: "root-model",
						state: { _esm: "root", value: 0 },
					},
				},
			},
			new ToolCallQueue({ callServerTool } as unknown as App),
			{
				getHostCapabilities: () => ({}),
				updateModelContext: vi.fn().mockResolvedValue({}),
			} as unknown as App,
			Promise.resolve(),
			(_bindingRuntime, model) => new FakeBinding(model, []),
		);
		const model = runtime.model("root-model");
		const request = new AbortController();
		const invoked = runtime
			.experimental(model, new AbortController().signal)
			.invoke("active", undefined, { signal: request.signal });
		await vi.waitFor(() => expect(callServerTool).toHaveBeenCalledOnce());

		request.abort(new DOMException("caller stopped", "AbortError"));
		await expect(invoked).rejects.toMatchObject({ name: "AbortError" });
		response.resolve({
			content: [],
			_meta: {
				anywidget: {
					messages: [
						{
							modelId: "root-model",
							data: { method: "update", state: { value: 9 } },
							buffers: [],
						},
					],
				},
			},
		});

		await vi.waitFor(() => expect(model.get("value")).toBe(9));
		await runtime.dispose();
	});

	test("disposes a runtime when a cancelled active command stays stalled", async () => {
		vi.useFakeTimers();
		const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
		try {
			const names: string[] = [];
			const callServerTool = vi.fn(async (request: { name: string }): Promise<CallToolResult> => {
				names.push(request.name);
				if (request.name === "anywidget_dispose") return { content: [] };
				return await new Promise<CallToolResult>(() => undefined);
			});
			const runtime = new WidgetRuntime(
				{
					instanceId: "instance-1",
					rootModelId: "root-model",
					models: {
						"root-model": { modelId: "root-model", state: { _esm: "root" } },
					},
				},
				new ToolCallQueue({ callServerTool } as unknown as App),
				{
					getHostCapabilities: () => ({}),
					updateModelContext: vi.fn().mockResolvedValue({}),
				} as unknown as App,
				Promise.resolve(),
				(_bindingRuntime, model) => new FakeBinding(model, []),
			);
			const request = new AbortController();
			const invoked = runtime
				.experimental(runtime.model("root-model"), new AbortController().signal)
				.invoke("stalled", undefined, { signal: request.signal });
			await vi.advanceTimersByTimeAsync(0);
			expect(names).toEqual(["anywidget_comm"]);

			request.abort(new DOMException("caller stopped", "AbortError"));
			await expect(invoked).rejects.toMatchObject({ name: "AbortError" });
			await vi.advanceTimersByTimeAsync(2999);
			expect(names).toEqual(["anywidget_comm"]);
			await vi.advanceTimersByTimeAsync(1);
			await vi.waitFor(() => expect(names).toContain("anywidget_dispose"));
			await runtime.dispose();
		} finally {
			consoleError.mockRestore();
			vi.useRealTimers();
		}
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
});
