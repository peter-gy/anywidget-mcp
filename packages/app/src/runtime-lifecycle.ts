import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

import { withTimeout } from "./abort";
import type { ToolArguments, ToolCalls } from "./tool-calls";
import { retryTransport } from "./transport";

export const RUNTIME_LIFECYCLE_TIMEOUT_MS = 3000;

export function disposeServerSession(
	calls: ToolCalls,
	sessionId: string,
	message: string,
	timeout = RUNTIME_LIFECYCLE_TIMEOUT_MS,
	operationId?: string,
): Promise<CallToolResult> {
	const signal = AbortSignal.timeout(timeout);
	const args: ToolArguments = { session_id: sessionId };
	if (operationId !== undefined) args.operation_id = operationId;
	// callNow bypasses stalled protocol work. Reuse this argument object across
	// transport retries so disposal keeps one replay identity.
	const request = retryTransport(
		() => calls.callNow("anywidget_dispose", args, signal),
		signal,
	).then((result) => {
		if (result.isError) throw new Error(toolErrorText(result, "AnyWidget disposal failed"));
		return result;
	});
	return withTimeout(request, timeout, message);
}

export function toolErrorText(result: CallToolResult, fallback = "AnyWidget comm failed"): string {
	const text = result.content.find((item) => item.type === "text");
	return text?.text ?? fallback;
}

export function randomId(): string {
	return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}
