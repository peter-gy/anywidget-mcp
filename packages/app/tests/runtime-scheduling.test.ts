import type { App } from "@modelcontextprotocol/ext-apps";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { describe, expect, test, vi } from "vite-plus/test";

import { WidgetRuntime } from "../src/app";
import { WidgetBinding } from "../src/binding";
import { ToolCallQueue } from "../src/tool-calls";
import { deferred, FakeBinding } from "./runtime-test-support";

describe("WidgetRuntime command scheduling", () => {
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
		await vi.waitFor(() => {
			const comms = callServerTool.mock.calls
				.map(([request]) => request)
				.filter((request) => request.name === "anywidget_comm");
			expect(comms.map((request) => request.arguments?.model_id)).toEqual([
				"model-a",
				"model-a",
				"model-a",
				"model-b",
			]);
		});

		const comms = callServerTool.mock.calls
			.map(([request]) => request)
			.filter((request) => request.name === "anywidget_comm")
			.map((request) => request.arguments);
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
			await vi.waitFor(() =>
				expect(callServerTool.mock.calls.map(([request]) => request.name)).toEqual([
					"anywidget_comm",
					"anywidget_comm",
					"anywidget_poll",
				]),
			);
			const requests = callServerTool.mock.calls.map(([request]) => request);
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

	test("runs initializer cleanup once when its protocol command fails", async () => {
		const rootId = "root-model";
		const childId = "dynamic-child";
		const cleanup = vi.fn();
		const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);
		const callServerTool = vi.fn(
			async (request: {
				name: string;
				arguments?: Record<string, unknown>;
			}): Promise<CallToolResult> => {
				if (request.name === "anywidget_dispose") return { content: [] };
				const data = request.arguments?.data as { method?: string } | undefined;
				if (data?.method === "custom") {
					return {
						content: [{ type: "text", text: "initializer command failed" }],
						isError: true,
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
					[rootId]: { modelId: rootId, state: { _esm: "root" } },
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
						initialize({ experimental }) {
							void experimental.invoke("initialize_child");
							return cleanup;
						},
					}),
				});
			},
		);

		try {
			await expect(
				runtime.send(rootId, { method: "update", state: {} }, [], "outer-operation"),
			).rejects.toThrow("initializer command failed");
			await runtime.dispose();
			expect(cleanup).toHaveBeenCalledOnce();
		} finally {
			consoleError.mockRestore();
		}
	});

	test("preserves outer, queued, and initializer command order with authoritative nested state", async () => {
		const rootId = "root-model";
		const childId = "dynamic-child";
		const initializeStarted = deferred<void>();
		const continueInitialize = deferred<void>();
		const order: string[] = [];
		const outerCustom: unknown[] = [];
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
							const [result] = await experimental.invoke("initialize_child");
							initialized.push(result);
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
		expect(initialized).toEqual([{ ready: true }]);
		expect(outerCustom).toEqual([{ source: "outer" }]);
		expect(runtime.model(childId).get("value")).toBe(2);
		await runtime.dispose();
	});
});
