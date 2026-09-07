import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { describe, expect, test, vi } from "vite-plus/test";

import { disposeServerSession } from "../src/app";
import { ToolCallQueue } from "../src/tool-calls";

describe("widget session disposal", () => {
	test("retries thrown transport failures with one bootstrap operation ID", async () => {
		vi.useFakeTimers();
		try {
			const callServerTool = vi
				.fn()
				.mockRejectedValueOnce(new Error("first response lost"))
				.mockRejectedValueOnce(new Error("second response lost"))
				.mockResolvedValue({ content: [] });
			const disposal = disposeServerSession(
				new ToolCallQueue({ callServerTool }),
				"bootstrap-capability",
				"disposal timed out",
				1000,
				"bootstrap-operation",
			);

			await vi.advanceTimersByTimeAsync(300);
			await expect(disposal).resolves.toEqual({ content: [] });

			const requests = callServerTool.mock.calls.map(([request]) => request);
			expect(requests).toHaveLength(3);
			expect(requests.map((request) => request.arguments)).toEqual([
				{
					session_id: "bootstrap-capability",
					operation_id: "bootstrap-operation",
				},
				{
					session_id: "bootstrap-capability",
					operation_id: "bootstrap-operation",
				},
				{
					session_id: "bootstrap-capability",
					operation_id: "bootstrap-operation",
				},
			]);
		} finally {
			vi.useRealTimers();
		}
	});

	test("rejects a disposal tool error without retrying it", async () => {
		const result: CallToolResult = {
			content: [{ type: "text", text: "session cannot be disposed" }],
			isError: true,
		};
		const callServerTool = vi.fn().mockResolvedValue(result);

		await expect(
			disposeServerSession(
				new ToolCallQueue({ callServerTool }),
				"instance-1",
				"disposal timed out",
			),
		).rejects.toThrow("session cannot be disposed");
		expect(callServerTool).toHaveBeenCalledOnce();
	});

	test("stops disposal retries when its timeout expires", async () => {
		const callServerTool = vi.fn().mockRejectedValue(new Error("offline"));

		await expect(
			disposeServerSession(
				new ToolCallQueue({ callServerTool }),
				"instance-1",
				"disposal timed out",
				20,
			),
		).rejects.toBeDefined();
		expect(callServerTool).toHaveBeenCalledOnce();
	});
});
