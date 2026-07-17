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

export interface RawModelPayload {
	modelId?: string;
	state?: State;
	sourceRefs?: unknown;
	buffers?: unknown[];
	bufferPaths?: JsonPath[];
}

export interface RawRuntimePayload {
	protocolVersion?: unknown;
	instanceId?: string;
	rootModelId?: string;
	loadingMessage?: unknown;
	sessionIdleTimeoutMs?: unknown;
	assetManifest?: unknown;
	models?: Record<string, RawModelPayload>;
	messages?: unknown;
	context?: ModelContextSnapshot;
}

export interface RawCommMessage {
	modelId?: string;
	data?: CommData;
	sourceRefs?: unknown;
	buffers?: unknown[];
}

export function resultAnywidget(result: CallToolResult): Record<string, unknown> | undefined {
	const meta = isRecord(result._meta) ? result._meta.anywidget : undefined;
	return isRecord(meta) ? meta : undefined;
}

export function resultMessages(result: CallToolResult): unknown {
	return resultAnywidget(result)?.messages;
}

export function resultModels(result: CallToolResult): unknown {
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
	return isRecord(meta) && typeof meta.contextError === "string" ? meta.contextError : undefined;
}

export function normalizeContext(value: unknown): ModelContextSnapshot | undefined {
	if (!isRecord(value)) return undefined;
	if (typeof value.version !== "number" || typeof value.tool !== "string" || !isRecord(value.state))
		return undefined;
	return {
		version: value.version,
		tool: value.tool,
		state: value.state,
	};
}

export function pollDelayLimit(value: unknown, fallback: number): number {
	if (value === undefined) return fallback;
	if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
		throw new Error("Widget session idle timeout must be a positive number");
	}
	return Math.max(1, Math.floor(value / 2));
}

export function hydrateRuntimePayload(
	payload: RawRuntimePayload,
	assets: Map<string, string>,
): RawRuntimePayload {
	return {
		...payload,
		models: hydrateRawModels(payload.models, assets) as Record<string, RawModelPayload>,
		messages: hydrateRawMessages(payload.messages, assets),
	};
}

export function sourceRefValues(models: unknown, messages: unknown): unknown[] {
	const values: unknown[] = [];
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

export function hydrateRawModels(models: unknown, assets: Map<string, string>): unknown {
	if (!isRecord(models)) return models;
	return Object.fromEntries(
		Object.entries(models).map(([modelId, value]) => {
			if (!isRecord(value) || !isRecord(value.state)) return [modelId, value];
			return [
				modelId,
				{
					...value,
					state: hydrateSources(value.state, value.sourceRefs, assets),
				},
			];
		}),
	);
}

export function hydrateRawMessages(messages: unknown, assets: Map<string, string>): unknown {
	if (!Array.isArray(messages)) return messages;
	return messages.map((value) => {
		if (!isRecord(value) || !isRecord(value.data) || !isRecord(value.data.state)) return value;
		return {
			...value,
			data: {
				...value.data,
				state: hydrateSources(value.data.state, value.sourceRefs, assets),
			},
		};
	});
}

export function normalizeMessages(value: unknown): RawCommMessage[] {
	if (value === undefined) return [];
	if (!Array.isArray(value)) throw new Error("Widget messages must be an array");
	return value as RawCommMessage[];
}

export function normalizeModels(models: Record<string, unknown> | undefined): ModelPayload[] {
	if (!isRecord(models)) throw new Error("Widget payload has no models");

	return Object.entries(models).map(([entryId, value]) => {
		if (!isRecord(value)) throw new Error(`Invalid widget model ${entryId}`);
		const raw = value as RawModelPayload;
		const modelId = requiredString(raw.modelId ?? entryId, "model ID");
		if (!isRecord(raw.state)) throw new Error(`Model ${modelId} has no state`);
		const bufferPaths = Array.isArray(raw.bufferPaths) ? raw.bufferPaths : [];
		const state = insertBuffers({ ...raw.state }, bufferPaths, decodeBuffers(raw.buffers));
		requiredString(state._esm, `ESM for model ${modelId}`);
		const cssValue = state._css;
		if (cssValue !== undefined && typeof cssValue !== "string") {
			throw new Error(`Invalid CSS for model ${modelId}`);
		}
		return { modelId, state };
	});
}

export function normalizeModelChanges(models: unknown): ModelPayload[] {
	if (models === undefined) return [];
	if (!isRecord(models)) throw new Error("Widget model changes must be an object");
	return normalizeModels(models);
}

export function decodeBuffers(raw: unknown[] | undefined): DataView[] {
	return (raw ?? []).map((value) => {
		if (typeof value !== "string") throw new Error("Widget buffer must be base64 text");
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

export function requiredString(value: unknown, label: string): string {
	if (typeof value !== "string" || value.length === 0) throw new Error(`Missing ${label}`);
	return value;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}
