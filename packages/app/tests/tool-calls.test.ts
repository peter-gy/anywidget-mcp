import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { describe, expect, test, vi } from "vite-plus/test";

import { ToolCallQueue, type ToolRequest } from "../src/tool-calls";

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

	test("lets a nested call narrow the active transaction signal", async () => {
		let activeSignal: AbortSignal | undefined;
		const callServerTool = vi.fn(
			(_request: ToolRequest, options?: { signal?: AbortSignal }): Promise<CallToolResult> => {
				activeSignal = options?.signal;
				return new Promise((_resolve, reject) => {
					activeSignal?.addEventListener("abort", () => reject(activeSignal?.reason), {
						once: true,
					});
				});
			},
		);
		const calls = new ToolCallQueue({ callServerTool });
		const outer = new AbortController();
		const inner = new AbortController();

		const result = calls.transaction(
			(call) => call("anywidget_assets", {}, inner.signal),
			outer.signal,
		);
		await vi.waitFor(() => expect(activeSignal).toBeDefined());
		inner.abort(new DOMException("initializer stopped", "AbortError"));

		await expect(result).rejects.toMatchObject({ name: "AbortError" });
		expect(activeSignal?.aborted).toBe(true);
		expect(outer.signal.aborted).toBe(false);
	});
});
