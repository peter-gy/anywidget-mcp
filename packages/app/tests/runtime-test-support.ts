import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

import { type RuntimeBinding } from "../src/binding";
import type { BridgeModel } from "../src/model";
import { ToolCallQueue } from "../src/tool-calls";

type ToolCall = (
	name: string,
	args: Record<string, unknown>,
	signal?: AbortSignal,
) => Promise<CallToolResult>;

export function fakeQueue(call: ToolCall, callNow: ToolCall): ToolCallQueue {
	return {
		call,
		callNow,
		transaction: <T>(task: (call: ToolCall) => Promise<T>, signal?: AbortSignal): Promise<T> => {
			signal?.throwIfAborted();
			return task((name, args, callSignal) => call(name, args, callSignal ?? signal));
		},
	} as unknown as ToolCallQueue;
}

export function deferred<T>(): { promise: Promise<T>; resolve: (value: T) => void } {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((done) => {
		resolve = done;
	});
	return { promise, resolve };
}

export class FakeBinding implements RuntimeBinding {
	private readonly ready: Promise<void>;
	private resolveReady!: () => void;

	constructor(
		readonly model: BridgeModel,
		private readonly events: string[],
	) {
		this.ready = new Promise<void>((resolve) => {
			this.resolveReady = resolve;
		});
	}

	async initialize(): Promise<void> {
		this.events.push(`initialize:${this.model.modelId}`);
		this.resolveReady();
	}

	async render(): Promise<void> {}

	async getExports(): Promise<unknown> {
		await this.ready;
		return { modelId: this.model.modelId };
	}

	async dispose(): Promise<void> {
		this.events.push(`dispose:${this.model.modelId}`);
	}
}
