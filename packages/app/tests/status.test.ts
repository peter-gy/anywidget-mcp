import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { describe, expect, test } from "vite-plus/test";

import {
	loadingMessageFromArguments,
	loadingMessageFromPayload,
	loadingMessageForTool,
	toolResultError,
	ReopenError,
	RuntimeStoppedError,
} from "../src/status";

describe("widget status", () => {
	test("uses model-supplied progress text while the tool runs", () => {
		expect(
			loadingMessageFromArguments({
				loading_message: "  Preparing a step-through view\n of quicksort  ",
			}),
		).toBe("Preparing a step-through view of quicksort");
	});

	test("falls back for non-text, unsafe, and oversized progress values", () => {
		expect(loadingMessageFromArguments({ loading_message: 42 })).toBeUndefined();
		expect(loadingMessageFromArguments({ loading_message: "Loading\u202eexe" })).toBeUndefined();
		expect(loadingMessageFromArguments({ loading_message: "x".repeat(121) })).toBeUndefined();
	});

	test("uses result metadata when a host mounts after tool execution", () => {
		const result: CallToolResult = {
			content: [],
			_meta: { anywidget: { loadingMessage: "Building the embedding atlas…" } },
		};

		expect(loadingMessageFromPayload(result._meta?.anywidget)).toBe(
			"Building the embedding atlas…",
		);
	});

	test("derives the fallback from the MCP tool title", () => {
		expect(loadingMessageForTool("Embedding Atlas")).toBe("Initializing Embedding Atlas…");
		expect(loadingMessageForTool(undefined)).toBe("Initializing widget…");
		expect(loadingMessageForTool("x".repeat(121))).toBe("Initializing widget…");
	});

	test("surfaces a failed primary tool result", () => {
		const result: CallToolResult = {
			isError: true,
			content: [{ type: "text", text: "The widget factory failed" }],
		};

		expect(toolResultError(result)).toBe("The widget factory failed");
		expect(toolResultError({ content: [] })).toBeUndefined();
		expect(
			toolResultError({
				isError: true,
				content: [
					{ type: "text", text: "" },
					{ type: "text", text: "Dataset unavailable." },
					{ type: "text", text: "Choose another dataset." },
				],
			}),
		).toBe("Dataset unavailable.\nChoose another dataset.");
	});
});

describe("recovery diagnostics", () => {
	test("preserves the original cause through recovery error wrapping", () => {
		const cause = new Error("Connection closed after sending tools/call");
		for (const error of [
			new ReopenError("explore", cause, "reopen_unconfirmed"),
			new RuntimeStoppedError(cause),
		]) {
			expect(error.cause).toBe(cause);
		}
	});
});
