export type JsonPath = Array<string | number>;
export type EventHandler = (...args: unknown[]) => void;
export type State = Record<string, unknown>;

export interface ModelPayload {
	modelId: string;
	state: State;
}

export interface CommData {
	method: string;
	state?: State;
	buffer_paths?: JsonPath[];
	content?: unknown;
	[key: string]: unknown;
}

export interface AnyModel {
	get(key: string): unknown;
	set(key: string, value: unknown): void;
	on(name: string, callback: EventHandler): void;
	off(name?: string | null, callback?: EventHandler | null): void;
	save_changes(): void;
	send(content: unknown, callbacks?: unknown, buffers?: Array<ArrayBuffer | ArrayBufferView>): void;
	widget_manager: {
		get_model(modelId: string): Promise<AnyModel>;
	};
}

export interface ModelRuntime {
	model(modelId: string): BridgeModel;
	enqueueUpdate(model: BridgeModel, state: Map<string, unknown>): void;
	enqueueCustom(model: BridgeModel, data: CommData, buffers: string[]): void;
}

interface ExtractedBuffers {
	value: State;
	buffers: string[];
	paths: JsonPath[];
}

interface PendingCustomMessage {
	content: unknown;
	buffers: DataView[];
}

const MAX_PENDING_CUSTOM_MESSAGES = 100;

export class BridgeModel implements AnyModel {
	readonly widget_manager = {
		get_model: async (modelId: string): Promise<AnyModel> => this.runtime.model(modelId),
	};

	readonly payload: ModelPayload;

	private readonly runtime: ModelRuntime;
	private readonly reportError: (error: unknown) => void;
	private readonly state: State;
	private readonly dirty = new Map<string, unknown>();
	private readonly handlers = new Map<string, Set<EventHandler>>();
	private readonly commandResponseHandlers = new Map<
		string,
		(content: Record<string, unknown>, buffers: DataView[]) => void
	>();
	private readonly pendingCustomMessages: PendingCustomMessage[] = [];
	private disposed = false;
	private readonly signal?: AbortSignal;
	private readonly abort = (): void => this.dispose();

	constructor(
		runtime: ModelRuntime,
		payload: ModelPayload,
		reportError: (error: unknown) => void,
		signal?: AbortSignal,
	) {
		this.runtime = runtime;
		this.payload = payload;
		this.reportError = reportError;
		this.state = payload.state;
		this.signal = signal;
		if (signal?.aborted) {
			this.disposed = true;
		} else {
			signal?.addEventListener("abort", this.abort, { once: true });
		}
	}

	get modelId(): string {
		return this.payload.modelId;
	}

	get(key: string): unknown {
		return this.state[key];
	}

	set(key: string, value: unknown): void {
		if (this.disposed) return;
		const changed = !Object.is(this.state[key], value);
		this.state[key] = value;
		this.dirty.set(key, value);
		if (changed) {
			this.emit(`change:${key}`);
			this.emit("change");
		}
	}

	on(name: string, callback: EventHandler): void {
		if (this.disposed) return;
		for (const eventName of eventNames(name)) {
			const handlers = this.handlers.get(eventName) ?? new Set<EventHandler>();
			handlers.add(callback);
			this.handlers.set(eventName, handlers);
			if (eventName === "msg:custom") this.flushPendingCustomMessages();
		}
	}

	off(name?: string | null, callback?: EventHandler | null): void {
		if (name == null) {
			if (callback == null) {
				this.handlers.clear();
				return;
			}
			for (const handlers of this.handlers.values()) handlers.delete(callback);
			return;
		}
		for (const eventName of eventNames(name)) {
			if (callback == null) {
				this.handlers.delete(eventName);
				continue;
			}
			this.handlers.get(eventName)?.delete(callback);
		}
	}

	onCommandResponse(
		id: string,
		callback: (content: Record<string, unknown>, buffers: DataView[]) => void,
	): () => void {
		if (this.disposed) return () => undefined;
		this.commandResponseHandlers.set(id, callback);
		return () => {
			if (this.commandResponseHandlers.get(id) === callback) {
				this.commandResponseHandlers.delete(id);
			}
		};
	}

	save_changes(): void {
		if (this.disposed || this.dirty.size === 0) return;

		let state: Map<string, unknown>;
		try {
			state = new Map<string, unknown>();
			for (const [key, value] of this.dirty) state.set(key, structuredClone(value));
		} catch (error) {
			this.reportError(error);
			return;
		}
		this.dirty.clear();
		this.runtime.enqueueUpdate(this, state);
	}

	send(
		content: unknown,
		_callbacks?: unknown,
		buffers: Array<ArrayBuffer | ArrayBufferView> = [],
	): void {
		if (this.disposed) return;
		try {
			const serialized = serializeCustom(content, buffers);
			this.runtime.enqueueCustom(this, serialized.data, serialized.buffers);
		} catch (error) {
			this.reportError(error);
		}
	}

	dispose(): void {
		if (this.disposed) return;
		this.disposed = true;
		this.signal?.removeEventListener("abort", this.abort);
		this.dirty.clear();
		this.handlers.clear();
		this.commandResponseHandlers.clear();
		this.pendingCustomMessages.length = 0;
	}

	receive(data: CommData, buffers: DataView[]): void {
		if (this.disposed) return;
		if (data.method === "custom") {
			// Command responses belong to experimental.invoke and must not enter the
			// widget-visible msg:custom queue.
			if (isRecord(data.content) && data.content.kind === "anywidget-command-response") {
				const id = data.content.id;
				if (typeof id === "string") {
					const handler = this.commandResponseHandlers.get(id);
					if (handler) handler(data.content, buffers);
				}
				return;
			}
			if ((this.handlers.get("msg:custom")?.size ?? 0) === 0) {
				if (this.pendingCustomMessages.length >= MAX_PENDING_CUSTOM_MESSAGES) {
					throw new Error(`Pending custom messages overflowed for model ${this.modelId}`);
				}
				this.pendingCustomMessages.push({ content: data.content, buffers });
				return;
			}
			this.emit("msg:custom", data.content, buffers);
			return;
		}
		// The local model already holds the value confirmed by an echo. Validator
		// and observer changes arrive as update messages from the Python model.
		if (data.method === "echo_update") return;
		if (data.method !== "update") return;

		if (!isRecord(data.state)) return;
		const next = insertBuffers(data.state, data.buffer_paths, buffers);
		let changed = false;
		for (const [key, value] of Object.entries(next)) {
			if (Object.is(this.state[key], value)) continue;
			this.state[key] = value;
			changed = true;
			this.emit(`change:${key}`);
		}
		if (changed) this.emit("change");
	}

	private emit(name: string, ...args: unknown[]): void {
		for (const handler of Array.from(this.handlers.get(name) ?? [])) handler(...args);
	}

	private flushPendingCustomMessages(): void {
		while (
			this.pendingCustomMessages.length > 0 &&
			(this.handlers.get("msg:custom")?.size ?? 0) > 0
		) {
			const message = this.pendingCustomMessages.shift();
			if (message) this.emit("msg:custom", message.content, message.buffers);
		}
	}
}

export function eventNames(name: string): string[] {
	return name.trim().split(/\s+/).filter(Boolean);
}

export function scopedModel(model: BridgeModel, signal: AbortSignal): AnyModel {
	const handlers = new Map<string, Set<EventHandler>>();
	let active = !signal.aborted;

	const remove = (name: string, callback: EventHandler): void => {
		model.off(name, callback);
		const eventHandlers = handlers.get(name);
		eventHandlers?.delete(callback);
		if (eventHandlers?.size === 0) handlers.delete(name);
	};

	const clear = (): void => {
		for (const [name, eventHandlers] of handlers) {
			for (const callback of eventHandlers) model.off(name, callback);
		}
		handlers.clear();
	};

	const abort = (): void => {
		active = false;
		clear();
	};

	if (active) signal.addEventListener("abort", abort, { once: true });
	return {
		get: (key) => (active ? model.get(key) : undefined),
		set: (key, value) => {
			if (active) model.set(key, value);
		},
		on(name, callback) {
			if (!active) return;
			for (const eventName of eventNames(name)) {
				model.on(eventName, callback);
				const eventHandlers = handlers.get(eventName) ?? new Set<EventHandler>();
				eventHandlers.add(callback);
				handlers.set(eventName, eventHandlers);
			}
		},
		off(name, callback) {
			if (!active) return;
			if (name == null) {
				if (callback == null) {
					clear();
					return;
				}
				for (const [eventName, eventHandlers] of handlers) {
					if (eventHandlers.has(callback)) remove(eventName, callback);
				}
				return;
			}
			for (const eventName of eventNames(name)) {
				if (callback == null) {
					for (const eventHandler of Array.from(handlers.get(eventName) ?? [])) {
						remove(eventName, eventHandler);
					}
					continue;
				}
				if (handlers.get(eventName)?.has(callback)) remove(eventName, callback);
			}
		},
		save_changes: () => {
			if (active) model.save_changes();
		},
		send: (content, callbacks, buffers) => {
			if (active) model.send(content, callbacks, buffers);
		},
		widget_manager: {
			async get_model(modelId) {
				if (!active) throw abortReason(signal);
				const child = await model.widget_manager.get_model(modelId);
				if (!active) throw abortReason(signal);
				return child instanceof BridgeModel ? scopedModel(child, signal) : child;
			},
		},
	};
}

function abortReason(signal: AbortSignal): unknown {
	return signal.reason ?? new DOMException("Model scope is closed", "AbortError");
}

export function serializeUpdate(state: ReadonlyMap<string, unknown>): {
	data: CommData;
	buffers: string[];
} {
	const extracted = extractBuffers(Object.fromEntries(state));
	return {
		data: {
			method: "update",
			state: extracted.value,
			buffer_paths: extracted.paths,
		},
		buffers: extracted.buffers,
	};
}

export function serializeCustom(
	content: unknown,
	buffers: Array<ArrayBuffer | ArrayBufferView> = [],
): { data: CommData; buffers: string[] } {
	return {
		data: { method: "custom", content: structuredClone(content) },
		buffers: buffers.map((buffer) => encodeBuffer(buffer)),
	};
}

function extractBuffers(state: State): ExtractedBuffers {
	const buffers: string[] = [];
	const paths: JsonPath[] = [];
	const removed = Symbol("buffer");

	const visit = (value: unknown, path: JsonPath): unknown => {
		if (value instanceof ArrayBuffer || ArrayBuffer.isView(value)) {
			buffers.push(encodeBuffer(value));
			paths.push(path);
			return removed;
		}
		if (Array.isArray(value)) {
			return value.map((item, index) => {
				const next = visit(item, [...path, index]);
				return next === removed ? null : next;
			});
		}
		if (isRecord(value)) {
			const entries: Array<[string, unknown]> = [];
			for (const [key, item] of Object.entries(value)) {
				const next = visit(item, [...path, key]);
				if (next !== removed) entries.push([key, next]);
			}
			return Object.fromEntries(entries);
		}
		return value;
	};

	const value = visit(state, []);
	if (value === removed) throw new Error("Widget state must be an object");
	return { value: value as State, buffers, paths };
}

export function insertBuffers(
	state: State,
	paths: JsonPath[] | undefined,
	buffers: DataView[],
): State {
	const value = state;
	for (const [index, path] of (paths ?? []).entries()) {
		const buffer = buffers[index];
		if (!buffer) continue;
		setAtPath(value, path, buffer);
	}
	return value;
}

function setAtPath(root: State, path: JsonPath, value: unknown): void {
	if (path.length === 0) throw new Error("Buffer path cannot be empty");
	let target: unknown = root;
	for (const part of path.slice(0, -1)) {
		if (!isRecord(target) && !Array.isArray(target)) {
			throw new Error(`Invalid buffer path ${JSON.stringify(path)}`);
		}
		target = target[part as keyof typeof target];
	}
	const key = path[path.length - 1];
	if (key === undefined || (!isRecord(target) && !Array.isArray(target))) {
		throw new Error(`Invalid buffer path ${JSON.stringify(path)}`);
	}
	const mutable = target as Record<string | number, unknown>;
	mutable[key] = value;
}

function encodeBuffer(buffer: ArrayBuffer | ArrayBufferView): string {
	const bytes =
		buffer instanceof ArrayBuffer
			? new Uint8Array(buffer)
			: new Uint8Array(buffer.buffer, buffer.byteOffset, buffer.byteLength);
	let binary = "";
	const chunkSize = 0x8000;
	for (let offset = 0; offset < bytes.length; offset += chunkSize) {
		binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
	}
	return btoa(binary);
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}
