import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { describe, expect, test, vi } from "vite-plus/test";

import { delivery } from "./attachment-test-support";

import { loadWidgetRuntime, parseToolLaunch, ToolResultGate } from "../src/runtime-results";

const bootstrapId = "a".repeat(32);

const primaryResult: CallToolResult = {
	content: [
		{
			type: "text",
			text: "Opened widget.",
		},
		{ type: "text", text: `urn:anywidget-mcp:bootstrap:${bootstrapId}` },
	],
	_meta: { ui: { resourceUri: "ui://anywidget-mcp/app.html" } },
};

const bootstrapPayload = { rootModelId: "root-model", sessionIdleTimeoutMs: 900_000, models: {} };
const bootstrapResult = delivery(bootstrapPayload);

describe("widget launch metadata", () => {
	test("reads the bootstrap ID from the marker text", () => {
		expect(parseToolLaunch(primaryResult)).toEqual({
			kind: "bootstrap",
			bootstrapId,
		});
	});

	test("ignores results that do not launch a widget", () => {
		for (const result of [
			{ content: [] },
			{ content: [], structuredContent: { disposed: true } },
			bootstrapResult,
			{ content: [], _meta: { anywidget: { messages: [] } } },
		] satisfies CallToolResult[]) {
			expect(parseToolLaunch(result)).toEqual({ kind: "other" });
		}
	});

	test("rejects malformed and conflicting bootstrap markers", () => {
		expect(
			parseToolLaunch({
				content: [{ type: "text", text: "urn:anywidget-mcp:bootstrap:not-an-id" }],
			}),
		).toMatchObject({ kind: "malformed" });
		expect(
			parseToolLaunch({
				content: [
					...primaryResult.content,
					{
						type: "text",
						text: `urn:anywidget-mcp:bootstrap:${"b".repeat(32)}`,
					},
				],
			}),
		).toMatchObject({ kind: "malformed" });
	});
});

describe("widget bootstrap", () => {
	test("loads the runtime through the app-only bootstrap tool", async () => {
		const launch = parseToolLaunch(primaryResult);
		const call = vi.fn().mockResolvedValue(bootstrapResult);

		await expect(loadWidgetRuntime(launch, call, "operation-1")).resolves.toEqual({
			result: bootstrapResult,
			payload: { ...bootstrapPayload, protocolVersion: 3, instanceId: "session-1" },
		});
		expect(call).toHaveBeenCalledWith("anywidget_bootstrap", {
			bootstrap_id: bootstrapId,
			operation_id: "operation-1",
		});
	});

	test("loads a session whose lifetime is controlled by explicit disposal", async () => {
		const result = delivery({ rootModelId: "root", models: {} });
		await expect(
			loadWidgetRuntime(parseToolLaunch(primaryResult), async () => result),
		).resolves.toEqual({
			result,
			payload: { rootModelId: "root", models: {}, protocolVersion: 3, instanceId: "session-1" },
		});
	});

	test("retries a rejected bootstrap transport with the same operation ID", async () => {
		vi.useFakeTimers();
		try {
			const launch = parseToolLaunch(primaryResult);
			const call = vi
				.fn()
				.mockRejectedValueOnce(new Error("bootstrap response lost"))
				.mockResolvedValueOnce(bootstrapResult);

			const loaded = loadWidgetRuntime(launch, call, "operation-1");
			await vi.advanceTimersByTimeAsync(100);

			await expect(loaded).resolves.toMatchObject({ result: bootstrapResult });
			expect(call).toHaveBeenCalledTimes(2);
			for (const invocation of call.mock.calls) {
				expect(invocation).toEqual([
					"anywidget_bootstrap",
					{
						bootstrap_id: bootstrapId,
						operation_id: "operation-1",
					},
				]);
			}
		} finally {
			vi.useRealTimers();
		}
	});

	test("stops a bootstrap retry when its launch is aborted", async () => {
		vi.useFakeTimers();
		try {
			const controller = new AbortController();
			const reason = new DOMException("Widget result superseded", "AbortError");
			const call = vi.fn().mockRejectedValue(new Error("bootstrap response lost"));
			const loaded = loadWidgetRuntime(
				parseToolLaunch(primaryResult),
				call,
				"operation-1",
				controller.signal,
			);
			await Promise.resolve();

			controller.abort(reason);

			await expect(loaded).rejects.toBe(reason);
			expect(call).toHaveBeenCalledOnce();
		} finally {
			vi.useRealTimers();
		}
	});

	test("surfaces a bootstrap tool error", async () => {
		const failed: CallToolResult = {
			isError: true,
			content: [{ type: "text", text: "Widget session not found" }],
		};

		await expect(
			loadWidgetRuntime(parseToolLaunch(primaryResult), async () => failed),
		).rejects.toThrow("Widget session not found");
	});

	test("requires a complete version-3 runtime", async () => {
		const launch = parseToolLaunch(primaryResult);
		const cases: Array<[CallToolResult, string]> = [
			[{ content: [] }, "no runtime data"],
			[
				{
					...bootstrapResult,
					_meta: {
						anywidget: {
							protocolVersion: 99,
							instanceId: "session-1",
							rootModelId: "root-model",
						},
					},
				},
				"protocol version",
			],
			[
				{
					...bootstrapResult,
					_meta: {
						anywidget: { protocolVersion: 3, rootModelId: "root-model" },
					},
				},
				"no instance ID",
			],
			[
				{
					...bootstrapResult,
					_meta: {
						anywidget: { protocolVersion: 3, instanceId: "session-1", payload: {} },
					},
				},
				"no root model ID",
			],
			[
				{
					...bootstrapResult,
					_meta: {
						anywidget: {
							protocolVersion: 3,
							instanceId: "session-1",
							payload: { rootModelId: "root-model", sessionIdleTimeoutMs: 0 },
						},
					},
				},
				"session idle timeout",
			],
		];

		await Promise.all(
			cases.map(([result, message]) =>
				expect(loadWidgetRuntime(launch, async () => result)).rejects.toThrow(message),
			),
		);
	});
});

describe("tool-result replay guard", () => {
	test("does not replace a pending or mounted session with its replay", () => {
		const gate = new ToolResultGate();
		const scheduled: AbortController[] = [];
		const schedule = (controller: AbortController) => scheduled.push(controller);

		expect(gate.deliver("first-session", schedule)).toBe(true);
		expect(gate.acceptedLaunch).toBe(true);
		expect(gate.deliver("first-session", schedule)).toBe(false);
		expect(scheduled).toHaveLength(1);
		expect(scheduled[0]?.signal.aborted).toBe(false);

		expect(gate.deliver("second-session", schedule)).toBe(true);
		expect(scheduled).toHaveLength(2);
		expect(scheduled[0]?.signal.aborted).toBe(true);
		expect(scheduled[1]?.signal.aborted).toBe(false);

		expect(gate.deliver("second-session", schedule)).toBe(false);
		expect(gate.deliver("first-session", schedule)).toBe(false);
		expect(scheduled).toHaveLength(2);
		expect(scheduled[1]?.signal.aborted).toBe(false);
	});
});
