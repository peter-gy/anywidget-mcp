import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { describe, expect, test, vi } from "vite-plus/test";

import { ToolCallQueue } from "../src/tool-calls";

function deferred<T>(): {
	promise: Promise<T>;
	resolve: (value: T) => void;
} {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((done) => {
		resolve = done;
	});
	return { promise, resolve };
}

const emptyResult: CallToolResult = { content: [] };

describe("ToolCallQueue cancellation", () => {
	test("passes the request signal to the active server tool call", async () => {
		const callServerTool = vi.fn().mockResolvedValue(emptyResult);
		const calls = new ToolCallQueue({ callServerTool });
		const controller = new AbortController();

		await calls.call("anywidget_poll", { instance_id: "widget-1" }, controller.signal);

		expect(callServerTool).toHaveBeenCalledWith(
			{
				name: "anywidget_poll",
				arguments: { instance_id: "widget-1" },
			},
			{ signal: controller.signal },
		);
	});

	test("skips queued work when its signal aborts before dispatch", async () => {
		const first = deferred<CallToolResult>();
		const callServerTool = vi.fn().mockReturnValueOnce(first.promise);
		const calls = new ToolCallQueue({ callServerTool });
		const controller = new AbortController();

		const active = calls.call("first", {});
		const cancelled = calls.call("second", {}, controller.signal);
		controller.abort(new Error("runtime disposed"));
		first.resolve(emptyResult);

		await active;
		await expect(cancelled).rejects.toThrow("runtime disposed");
		expect(callServerTool).toHaveBeenCalledTimes(1);
	});

	test("releases the queue when an active caller ignores cancellation", async () => {
		const never = new Promise<CallToolResult>(() => undefined);
		const callServerTool = vi.fn().mockReturnValueOnce(never).mockResolvedValueOnce(emptyResult);
		const calls = new ToolCallQueue({ callServerTool });
		const controller = new AbortController();

		const first = calls.call("first", {}, controller.signal);
		await Promise.resolve();
		const second = calls.call("second", {});
		const cancelled = expect(first).rejects.toThrow("runtime disposed");
		controller.abort(new Error("runtime disposed"));

		await cancelled;
		await expect(second).resolves.toBe(emptyResult);
		expect(callServerTool.mock.calls.map(([request]) => request.name)).toEqual(["first", "second"]);
	});

	test("holds the queue through transaction result processing", async () => {
		const processing = deferred<void>();
		const callServerTool = vi.fn().mockResolvedValue(emptyResult);
		const calls = new ToolCallQueue({ callServerTool });

		const first = calls.transaction(async (call) => {
			const result = await call("first", {});
			await processing.promise;
			return result;
		});
		const second = calls.call("second", {});
		await Promise.resolve();
		await Promise.resolve();

		expect(callServerTool).toHaveBeenCalledTimes(1);
		processing.resolve(undefined);
		await Promise.all([first, second]);
		expect(callServerTool.mock.calls.map(([request]) => request.name)).toEqual(["first", "second"]);
	});

	test("dispatches a direct teardown while queued work is still active", async () => {
		const active = deferred<CallToolResult>();
		const callServerTool = vi
			.fn()
			.mockReturnValueOnce(active.promise)
			.mockResolvedValueOnce(emptyResult);
		const calls = new ToolCallQueue({ callServerTool });

		void calls.call("anywidget_poll", { instance_id: "widget-1" });
		await Promise.resolve();
		await calls.callNow("anywidget_dispose", { instance_id: "widget-1" });

		expect(callServerTool.mock.calls.map(([request]) => request.name)).toEqual([
			"anywidget_poll",
			"anywidget_dispose",
		]);
	});
});
