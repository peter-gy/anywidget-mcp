import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { describe, expect, test } from "vite-plus/test";

import {
	DEFAULT_LOADING_MESSAGE,
	loadingMessageFromArguments,
	loadingMessageFromResult,
	loadingMessageForTool,
	toolResultError,
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

		expect(loadingMessageFromResult(result)).toBe("Building the embedding atlas…");
	});

	test("derives the fallback from the MCP tool title", () => {
		expect(loadingMessageForTool("Embedding Atlas")).toBe("Initializing Embedding Atlas…");
		expect(loadingMessageForTool(undefined)).toBe(DEFAULT_LOADING_MESSAGE);
		expect(loadingMessageForTool("x".repeat(121))).toBe(DEFAULT_LOADING_MESSAGE);
	});

	test("surfaces a failed primary tool result", () => {
		const result: CallToolResult = {
			isError: true,
			content: [{ type: "text", text: "The widget factory failed" }],
		};

		expect(toolResultError(result)).toBe("The widget factory failed");
		expect(toolResultError({ content: [] })).toBeUndefined();
	});
});
