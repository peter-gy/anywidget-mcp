import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

import { hydrateSources } from "./assets";
import type { ModelContextSnapshot } from "./context";
import {
	insertBuffers,
	type CommData,
	type JsonPath,
	type ModelPayload,
	type State,
} from "./model";
import {
	isNumber,
	isRecord,
	isString,
	type RuntimeRecord,
	type RuntimeValue,
} from "./runtime-value";

export interface RawModelPayload {
	modelId?: string;
	state?: State;
	sourceRefs?: RuntimeValue;
	buffers?: RuntimeValue[];
	bufferPaths?: JsonPath[];
}

export interface RawRuntimePayload {
	protocolVersion?: RuntimeValue;
	instanceId?: string;
	rootModelId?: string;
	loadingMessage?: RuntimeValue;
	sessionIdleTimeoutMs?: RuntimeValue;
	assetManifest?: RuntimeValue;
	models?: RuntimeValue;
	messages?: RuntimeValue;
	context?: RuntimeValue;
}

export interface RawCommMessage {
	modelId?: string;
	data?: CommData;
	sourceRefs?: RuntimeValue;
	buffers?: RuntimeValue[];
}

export function resultAnywidget(result: CallToolResult): RuntimeRecord | undefined {
	const meta = isRecord(result._meta) ? result._meta.anywidget : undefined;
	return isRecord(meta) ? meta : undefined;
}

export function resultMessages(result: CallToolResult): RuntimeValue {
	return resultAnywidget(result)?.messages;
}

export function resultModels(result: CallToolResult): RuntimeValue {
	return resultAnywidget(result)?.models;
}

export function resultRemovedModelIds(result: CallToolResult): string[] {
	const meta = isRecord(result._meta) ? result._meta.anywidget : undefined;
	if (!isRecord(meta) || meta.removedModelIds === undefined) return [];
	if (!Array.isArray(meta.removedModelIds)) {
		throw new Error("Widget model removals must be an array");
	}
	return meta.removedModelIds.map((value) => requiredString(value, "removed model ID"));
}

export function resultContext(result: CallToolResult): ModelContextSnapshot | undefined {
	const meta = isRecord(result._meta) ? result._meta.anywidget : undefined;
	if (!isRecord(meta)) return undefined;
	return normalizeContext(meta.context);
}

export function resultContextError(result: CallToolResult): string | undefined {
	const meta = isRecord(result._meta) ? result._meta.anywidget : undefined;
	return isRecord(meta) && isString(meta.contextError) ? meta.contextError : undefined;
}

export function normalizeContext<Value>(value: Value): ModelContextSnapshot | undefined {
	if (!isRecord(value)) return undefined;
	if (!isNumber(value.version) || !isString(value.tool) || !isRecord(value.state)) return undefined;
	return {
		version: value.version,
		tool: value.tool,
		state: value.state,
	};
}

export function pollDelayLimit<Value>(value: Value, fallback: number): number {
	if (value === undefined) return fallback;
	if (!isNumber(value) || !Number.isFinite(value) || value <= 0) {
		throw new Error("Widget session idle timeout must be a positive number");
	}
	// Cap the client wait at half the server idle timeout so the next poll is
	// scheduled before lease expiry.
	return Math.max(1, Math.floor(value / 2));
}

export function hydrateRuntimePayload(
	payload: RawRuntimePayload,
	assets: Map<string, string>,
): RawRuntimePayload {
	return {
		...payload,
		models: hydrateRawModels(payload.models, assets),
		messages: hydrateRawMessages(payload.messages, assets),
	};
}

export function sourceRefValues(models: RuntimeValue, messages: RuntimeValue): RuntimeValue[] {
	const values: RuntimeValue[] = [];
	if (isRecord(models)) {
		for (const model of Object.values(models)) {
			if (isRecord(model)) values.push(model.sourceRefs);
		}
	}
	if (Array.isArray(messages)) {
		for (const message of messages) {
			if (isRecord(message)) values.push(message.sourceRefs);
		}
	}
	return values;
}

export function hydrateRawModels(models: RuntimeValue, assets: Map<string, string>): RuntimeValue {
	if (!isRecord(models)) return models;
	return Object.fromEntries(
		Object.entries(models).map(([modelId, value]) => {
			if (!isRecord(value) || !isRecord(value.state)) return [modelId, value];
			const model: RuntimeRecord = value;
			const state: RuntimeRecord = value.state;
			return [
				modelId,
				{
					...model,
					state: hydrateSources(state, model.sourceRefs, assets),
				},
			];
		}),
	);
}

export function hydrateRawMessages(
	messages: RuntimeValue,
	assets: Map<string, string>,
): RuntimeValue {
	if (!Array.isArray(messages)) return messages;
	return messages.map((value) => {
		if (!isRecord(value) || !isRecord(value.data) || !isRecord(value.data.state)) return value;
		const message: RuntimeRecord = value;
		const data: RuntimeRecord = value.data;
		const state: RuntimeRecord = value.data.state;
		return {
			...message,
			data: {
				...data,
				state: hydrateSources(state, message.sourceRefs, assets),
			},
		};
	});
}

export function normalizeMessages<Value>(value: Value): RawCommMessage[] {
	if (value === undefined) return [];
	if (!Array.isArray(value)) throw new Error("Widget messages must be an array");
	return value.map((message, index) => normalizeMessage(message, index));
}

export function normalizeModels<Value>(models: Value): ModelPayload[] {
	if (!isRecord(models)) throw new Error("Widget payload has no models");

	return Object.entries(models).map(([entryId, value]) => {
		if (!isRecord(value)) throw new Error(`Invalid widget model ${entryId}`);
		const model: RuntimeRecord = value;
		const modelId = requiredString(model.modelId ?? entryId, "model ID");
		if (!isRecord(model.state)) throw new Error(`Model ${modelId} has no state`);
		const sourceState: RuntimeRecord = model.state;
		const bufferPaths = normalizeBufferPaths(model.bufferPaths);
		const state = insertBuffers({ ...sourceState }, bufferPaths, decodeBuffers(model.buffers));
		requiredString(state._esm, `ESM for model ${modelId}`);
		const cssValue = state._css;
		if (cssValue !== undefined && !isString(cssValue)) {
			throw new Error(`Invalid CSS for model ${modelId}`);
		}
		return { modelId, state };
	});
}

export function normalizeModelChanges<Value>(models: Value): ModelPayload[] {
	if (models === undefined) return [];
	if (!isRecord(models)) throw new Error("Widget model changes must be an object");
	return normalizeModels(models);
}

export function decodeBuffers<Value>(raw: Value): DataView[] {
	if (raw === undefined) return [];
	if (!Array.isArray(raw)) throw new Error("Widget buffers must be an array");
	return raw.map((value) => {
		if (!isString(value)) throw new Error("Widget buffer must be base64 text");
		const binary = atob(value);
		const bytes = new Uint8Array(binary.length);
		for (let index = 0; index < binary.length; index += 1) {
			bytes[index] = binary.charCodeAt(index);
		}
		return new DataView(bytes.buffer);
	});
}

export function parseWidgetRef(ref: string): string {
	const prefix = "anywidget:";
	if (!ref.startsWith(prefix) || ref.length === prefix.length) {
		throw new Error(`Invalid anywidget reference ${ref}`);
	}
	return ref.slice(prefix.length);
}

export function requiredString<Value>(value: Value, label: string): string {
	if (!isString(value) || value.length === 0) throw new Error(`Missing ${label}`);
	return value;
}

function normalizeMessage<Value>(value: Value, index: number): RawCommMessage {
	if (!isRecord(value)) throw new Error(`Invalid widget message ${index}`);
	const modelId =
		value.modelId === undefined ? undefined : requiredString(value.modelId, "model ID");
	const data = value.data === undefined ? undefined : normalizeCommData(value.data, index);
	const buffers = normalizeRuntimeArray(value.buffers, `buffers for widget message ${index}`);
	return { modelId, data, sourceRefs: value.sourceRefs, buffers };
}

function normalizeCommData<Value>(value: Value, index: number): CommData {
	if (!isRecord(value) || !isString(value.method)) {
		throw new Error(`Invalid widget message data ${index}`);
	}
	return {
		...value,
		method: value.method,
		buffer_paths: normalizeBufferPaths(value.buffer_paths),
	};
}

function normalizeBufferPaths<Value>(value: Value): JsonPath[] {
	if (value === undefined) return [];
	if (!Array.isArray(value)) throw new Error("Widget buffer paths must be an array");
	return value.map((path) => {
		if (!Array.isArray(path) || !path.every((part) => isString(part) || isNumber(part))) {
			throw new Error("Widget buffer path must contain strings and numbers");
		}
		return path;
	});
}

function normalizeRuntimeArray<Value>(value: Value, label: string): RuntimeValue[] | undefined {
	if (value === undefined) return undefined;
	if (!Array.isArray(value)) throw new Error(`${label} must be an array`);
	return value;
}
