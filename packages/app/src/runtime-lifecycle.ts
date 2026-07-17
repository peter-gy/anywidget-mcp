import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

import { ToolCallQueue } from "./tool-calls";
import { retryTransport } from "./transport";

export const RUNTIME_LIFECYCLE_TIMEOUT_MS = 3000;

export function disposeServerSession(
	calls: ToolCallQueue,
	sessionId: string,
	message: string,
	timeout = RUNTIME_LIFECYCLE_TIMEOUT_MS,
	operationId?: string,
): Promise<CallToolResult> {
	const signal = AbortSignal.timeout(timeout);
	const args: Record<string, unknown> = { session_id: sessionId };
	if (operationId !== undefined) args.operation_id = operationId;
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

export function abortReason(signal: AbortSignal): unknown {
	return signal.reason ?? new DOMException("Widget runtime is closed", "AbortError");
}

export function abortable<T>(task: Promise<T>, signal: AbortSignal): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		let settled = false;
		const finish = (callback: () => void): void => {
			if (settled) return;
			settled = true;
			signal.removeEventListener("abort", abort);
			callback();
		};
		const abort = (): void => finish(() => reject(abortReason(signal)));

		if (signal.aborted) {
			abort();
		} else {
			signal.addEventListener("abort", abort, { once: true });
		}
		void task.then(
			(value) => finish(() => resolve(value)),
			(error: unknown) => finish(() => reject(error)),
		);
	});
}

export function withTimeout<T>(
	task: Promise<T>,
	milliseconds: number,
	message: string,
): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		const timeout = globalThis.setTimeout(() => reject(new Error(message)), milliseconds);
		void task.then(
			(value) => {
				globalThis.clearTimeout(timeout);
				resolve(value);
			},
			(error) => {
				globalThis.clearTimeout(timeout);
				reject(error);
			},
		);
	});
}
