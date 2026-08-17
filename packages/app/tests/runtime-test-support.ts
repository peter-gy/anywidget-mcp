import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

import { type RuntimeBinding } from "../src/binding";
import type { BridgeModel } from "../src/model";
import { isNumber, isRecord, isString, type RuntimeRecord } from "../src/runtime-value";
import type { ToolArguments, ToolCalls } from "../src/tool-calls";

type ToolCall = (
	name: string,
	args: ToolArguments,
	signal?: AbortSignal,
) => Promise<CallToolResult>;

export function fakeQueue(call: ToolCall, callNow: ToolCall): ToolCalls {
	return {
		call,
		callNow,
		transaction: <T>(task: (call: ToolCall) => Promise<T>, signal?: AbortSignal): Promise<T> => {
			signal?.throwIfAborted();
			return task((name, args, callSignal) => call(name, args, callSignal ?? signal));
		},
	};
}

export interface Deferred<T> {
	promise: Promise<T>;
	resolve(value: T): void;
}

export function deferred<T>(): Deferred<T> {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((done) => {
		resolve = done;
	});
	return { promise, resolve };
}

export function fixtureRecord<Value>(value: Value): RuntimeRecord {
	if (!isRecord(value)) throw new Error("Expected a record in test fixture");
	return value;
}

export function fixtureString<Value>(value: Value): string {
	if (!isString(value)) throw new Error("Expected a string in test fixture");
	return value;
}

export function fixtureTextValue<Value>(value: Value): string | number {
	if (!isString(value) && !isNumber(value)) {
		throw new Error("Expected text-compatible data in test fixture");
	}
	return value;
}

export function fixtureStrings<Value>(value: Value): string[] {
	if (!Array.isArray(value) || !value.every(isString)) {
		throw new Error("Expected a string array in test fixture");
	}
	return value;
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

	async getExports(): Promise<RuntimeRecord> {
		await this.ready;
		return { modelId: this.model.modelId };
	}

	async dispose(): Promise<void> {
		this.events.push(`dispose:${this.model.modelId}`);
	}
}
