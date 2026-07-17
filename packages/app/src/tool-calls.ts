import type { App } from "@modelcontextprotocol/ext-apps";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

type ServerToolCaller = Pick<App, "callServerTool">;
export type QueuedToolCall = (
	name: string,
	args: Record<string, unknown>,
	signal?: AbortSignal,
) => Promise<CallToolResult>;

export class ToolCallQueue {
	private tail = Promise.resolve();

	constructor(private readonly app: ServerToolCaller) {}

	call(name: string, args: Record<string, unknown>, signal?: AbortSignal): Promise<CallToolResult> {
		return this.transaction((call) => call(name, args), signal);
	}

	transaction<T>(task: (call: QueuedToolCall) => Promise<T>, signal?: AbortSignal): Promise<T> {
		const result = this.tail.then(() => {
			signal?.throwIfAborted();
			// Nested calls bypass this queue. Enqueuing them would place them behind the
			// transaction that is currently waiting for their result.
			const active = Promise.resolve(
				task((name, args, callSignal) =>
					this.invoke(name, args, combineSignals(signal, callSignal)),
				),
			);
			return signal ? abortable(active, signal) : active;
		});
		this.tail = result.then(
			() => undefined,
			() => undefined,
		);
		return result;
	}

	callNow(
		name: string,
		args: Record<string, unknown>,
		signal?: AbortSignal,
	): Promise<CallToolResult> {
		signal?.throwIfAborted();
		return this.invoke(name, args, signal);
	}

	private invoke(
		name: string,
		args: Record<string, unknown>,
		signal?: AbortSignal,
	): Promise<CallToolResult> {
		signal?.throwIfAborted();
		return this.app.callServerTool({ name, arguments: args }, signal ? { signal } : undefined);
	}
}

function combineSignals(
	outer: AbortSignal | undefined,
	inner: AbortSignal | undefined,
): AbortSignal | undefined {
	if (outer && inner && outer !== inner) return AbortSignal.any([outer, inner]);
	return inner ?? outer;
}

function abortable<T>(task: Promise<T>, signal: AbortSignal): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		let settled = false;
		const settle = (callback: () => void): void => {
			if (settled) return;
			settled = true;
			signal.removeEventListener("abort", abort);
			callback();
		};
		const abort = (): void => settle(() => reject(signal.reason));

		signal.addEventListener("abort", abort, { once: true });
		void task.then(
			(value) => settle(() => resolve(value)),
			(error: unknown) => settle(() => reject(error)),
		);
		if (signal.aborted) abort();
	});
}
