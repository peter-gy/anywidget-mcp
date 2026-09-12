import { toolResultFailure } from "./status";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

import { isNumber, isRecord, isString, type RuntimeRecord } from "./runtime-value";
import type { QueuedToolCall, ToolArguments } from "./tool-calls";
import { retryTransport } from "./transport";

export const PROTOCOL_VERSION = 3;
export const CHUNK_BYTES = 64 * 1024;

export interface BlobRef extends RuntimeRecord {
	id: string;
	byteLength: number;
}

export function requireProtocolVersion<Value>(value: Value): void {
	if (value !== PROTOCOL_VERSION) {
		throw new Error(
			`Widget payload protocol version ${String(value)} is incompatible with version ${PROTOCOL_VERSION}`,
		);
	}
}

export function normalizeBlobRef<Value>(value: Value): BlobRef {
	if (
		!isRecord(value) ||
		!isString(value.id) ||
		!/^sha256:[0-9a-f]{64}$/.test(value.id) ||
		!isNumber(value.byteLength) ||
		!Number.isSafeInteger(value.byteLength) ||
		value.byteLength < 0
	) {
		throw new Error("Invalid widget attachment reference");
	}
	return { id: value.id, byteLength: value.byteLength };
}

export async function blobRef(bytes: Uint8Array<ArrayBuffer>): Promise<BlobRef> {
	const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
	const hex = Array.from(new Uint8Array(digest), (value) =>
		value.toString(16).padStart(2, "0"),
	).join("");
	return { id: `sha256:${hex}`, byteLength: bytes.byteLength };
}

export function decodeChunk(value: string): Uint8Array<ArrayBuffer> {
	if (
		value.length > Math.ceil(CHUNK_BYTES / 3) * 4 ||
		!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)
	)
		throw new Error("Invalid widget attachment chunk encoding");
	const binary = atob(value);
	return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

export function encodeChunk(bytes: Uint8Array): string {
	let binary = "";
	for (let offset = 0; offset < bytes.length; offset += 0x8000)
		binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
	return btoa(binary);
}

export class AttachmentStore {
	private readonly entries = new Map<string, WeakRef<ArrayBuffer>>();
	private readonly collected = new FinalizationRegistry<{
		id: string;
		reference: WeakRef<ArrayBuffer>;
	}>(({ id, reference }) => {
		if (this.entries.get(id) === reference) this.entries.delete(id);
	});

	constructor(readonly instanceId: string) {}

	clear(): void {
		for (const reference of this.entries.values()) this.collected.unregister(reference);
		this.entries.clear();
	}

	private remember(id: string, buffer: ArrayBuffer): void {
		const previous = this.entries.get(id);
		if (previous) this.collected.unregister(previous);
		const reference = new WeakRef(buffer);
		this.entries.set(id, reference);
		this.collected.register(buffer, { id, reference }, reference);
	}

	async read(
		ref: BlobRef,
		call: QueuedToolCall,
		signal?: AbortSignal,
	): Promise<Uint8Array<ArrayBuffer>> {
		signal?.throwIfAborted();
		const cached = this.entries.get(ref.id)?.deref();
		let candidate: Uint8Array<ArrayBuffer> | undefined;
		if (cached && cached.byteLength === ref.byteLength) {
			// Models can mutate or transfer these buffers. Verify an isolated copy before reuse.
			try {
				candidate = new Uint8Array(cached.slice(0));
			} catch {
				// A transferred ArrayBuffer must be read again from its immutable attachment.
			}
		}
		if (candidate && (await blobRef(candidate)).id === ref.id) {
			signal?.throwIfAborted();
			this.remember(ref.id, candidate.buffer);
			return candidate;
		}

		const controller = new AbortController();
		const readSignal = signal ? AbortSignal.any([signal, controller.signal]) : controller.signal;
		const bytes = new Uint8Array(ref.byteLength);
		let nextOffset = 0;
		const readNext = async (): Promise<void> => {
			const offset = nextOffset;
			if (offset >= ref.byteLength) return;
			nextOffset += CHUNK_BYTES;
			const args = { instance_id: this.instanceId, blob_id: ref.id, offset };
			const result = await retryTransport(
				() => call("anywidget_read", args, readSignal),
				readSignal,
			);
			const meta = attachmentMeta(result);
			if (
				meta.id !== ref.id ||
				meta.offset !== offset ||
				meta.byteLength !== ref.byteLength ||
				!isString(meta.data)
			)
				throw new Error("Widget attachment chunk does not match its request");
			const chunk = decodeChunk(meta.data);
			if (chunk.byteLength !== Math.min(CHUNK_BYTES, ref.byteLength - offset))
				throw new Error("Widget attachment chunk is truncated or oversized");
			bytes.set(chunk, offset);
			await readNext();
		};
		try {
			await Promise.all(Array.from({ length: 4 }, () => readNext()));
		} catch (error) {
			controller.abort(error);
			throw error;
		}
		if ((await blobRef(bytes)).id !== ref.id)
			throw new Error(`Widget attachment failed verification for ${ref.id}`);
		signal?.throwIfAborted();
		this.remember(ref.id, bytes.buffer);
		return bytes;
	}

	async write(
		bytes: Uint8Array<ArrayBuffer>,
		operationId: number,
		call: QueuedToolCall,
		signal?: AbortSignal,
	): Promise<BlobRef> {
		signal?.throwIfAborted();
		const ref = await blobRef(bytes);
		const writeNext = async (offset: number): Promise<void> => {
			const chunk = bytes.subarray(offset, offset + CHUNK_BYTES);
			const next = offset + chunk.byteLength;
			const args = {
				instance_id: this.instanceId,
				blob_id: ref.id,
				operation_id: operationId,
				byte_length: ref.byteLength,
				offset,
				data: encodeChunk(chunk),
			};
			const result = await retryTransport(() => call("anywidget_write", args, signal), signal);
			const meta = attachmentMeta(result);
			if (
				meta.id !== ref.id ||
				meta.byteLength !== ref.byteLength ||
				!isNumber(meta.received) ||
				!Number.isSafeInteger(meta.received) ||
				meta.received < next ||
				meta.received > ref.byteLength ||
				meta.complete !== (meta.received === ref.byteLength)
			)
				throw new Error("Widget attachment upload acknowledgment does not match its chunk");
			if (meta.received < bytes.byteLength) await writeNext(meta.received);
		};
		await writeNext(0);
		signal?.throwIfAborted();
		return ref;
	}

	async commArguments(
		args: ToolArguments,
		call: QueuedToolCall,
		signal?: AbortSignal,
	): Promise<ToolArguments> {
		if (!Array.isArray(args.buffers)) throw new Error("Widget comm buffers must be an array");
		const operationId = args.operation_id;
		if (!isNumber(operationId) || !Number.isSafeInteger(operationId) || operationId <= 0)
			throw new Error("Widget comm requires an operation ID");
		const buffers = await args.buffers.reduce<Promise<BlobRef[]>>(async (pending, value) => {
			const refs = await pending;
			if (!(value instanceof ArrayBuffer))
				throw new Error("Widget comm buffer must be an ArrayBuffer");
			refs.push(await this.write(new Uint8Array(value), operationId, call, signal));
			return refs;
		}, Promise.resolve([]));
		const payload = { data: args.data, buffers };
		const bytes = new TextEncoder().encode(JSON.stringify(payload));
		const identity = {
			instance_id: args.instance_id,
			model_id: args.model_id,
			operation_id: args.operation_id,
			acknowledged_operation_id: args.acknowledged_operation_id,
		};
		return { ...identity, payload_ref: await this.write(bytes, operationId, call, signal) };
	}
}

export async function deliveryPayload(
	result: CallToolResult,
	store: AttachmentStore,
	call: QueuedToolCall,
	signal?: AbortSignal,
): Promise<RuntimeRecord> {
	const meta = attachmentMeta(result);
	if (meta.instanceId !== store.instanceId)
		throw new Error("Widget delivery has a different instance ID");
	if (Object.hasOwn(meta, "payload") === Object.hasOwn(meta, "payloadRef"))
		throw new Error("Widget delivery must contain one payload or payload reference");
	let payload = meta.payload;
	if (meta.payloadRef !== undefined) {
		const bytes = await store.read(normalizeBlobRef(meta.payloadRef), call, signal);
		const parsed: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
		if (!isRecord(parsed)) throw new Error("Widget delivery payload must be an object");
		payload = parsed;
	}
	if (!isRecord(payload)) throw new Error("Widget delivery payload must be an object");
	const record: RuntimeRecord = payload;
	return { ...record, protocolVersion: PROTOCOL_VERSION, instanceId: store.instanceId };
}

function attachmentMeta(result: CallToolResult): RuntimeRecord {
	if (result.isError) throw toolResultFailure(result);
	const meta = isRecord(result._meta) ? result._meta.anywidget : undefined;
	if (!isRecord(meta)) throw new Error("Widget response has no protocol metadata");
	requireProtocolVersion(meta.protocolVersion);
	return meta;
}
