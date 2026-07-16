import type { App } from "@modelcontextprotocol/ext-apps";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { describe, expect, test, vi } from "vite-plus/test";

import { WidgetRuntime } from "../src/app";
import type { RuntimeBinding } from "../src/binding";
import { ToolCallQueue } from "../src/tool-calls";
import { deferred, FakeBinding } from "./runtime-test-support";

describe("WidgetRuntime transport and polling", () => {
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
					arguments: {
						instance_id: "instance-1",
						operation_id: expect.any(String),
						acknowledged_model_ids: [],
					},
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
});
