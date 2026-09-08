import { AttachmentStore, blobRef, normalizeBlobRef, type BlobRef } from "./attachments";
import type { State } from "./model";
import { isRecord, type RuntimeValue } from "./runtime-value";
import type { QueuedToolCall } from "./tool-calls";

export interface SourceRefs {
	_esm?: BlobRef;
	_css?: BlobRef;
}
const CACHE_IO_TIMEOUT_MS = 500;
const CACHE_NAME = "anywidget-mcp-sources-v3";
const CACHE_KEY_PREFIX = "https://anywidget-mcp.invalid/sources/";

export function normalizeSourceRefs<Value>(value: Value): SourceRefs {
	if (value === undefined) return {};
	if (!isRecord(value)) throw new Error("Widget source references must be an object");
	const refs: SourceRefs = {};
	for (const [trait, ref] of Object.entries(value)) {
		if (trait !== "_esm" && trait !== "_css")
			throw new Error(`Unknown widget source reference ${trait}`);
		refs[trait] = normalizeBlobRef(ref);
	}
	return refs;
}

export async function resolveSources(
	refValues: RuntimeValue[],
	store: AttachmentStore,
	call: QueuedToolCall,
	signal?: AbortSignal,
): Promise<Map<string, string>> {
	const resolved = new Map<string, string>();
	const references = refValues.flatMap((value) => Object.values(normalizeSourceRefs(value)));
	await references.reduce<Promise<void>>(async (pending, ref) => {
		await pending;
		if (resolved.has(ref.id)) return;
		signal?.throwIfAborted();
		let source = await readPersistent(ref, signal);
		if (source === undefined) {
			const bytes = await store.read(ref, call, signal);
			source = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
			void writePersistent(ref, source);
		}
		signal?.throwIfAborted();
		resolved.set(ref.id, source);
	}, Promise.resolve());
	return resolved;
}

export function hydrateSources(
	state: State,
	refValue: RuntimeValue,
	sources: Map<string, string>,
): State {
	for (const trait of ["_esm", "_css"] as const) {
		if (Object.hasOwn(state, trait))
			throw new Error(`Widget source ${trait} must use a content-addressed reference`);
	}
	const hydrated = { ...state };
	for (const [trait, ref] of Object.entries(normalizeSourceRefs(refValue))) {
		const source = sources.get(ref.id);
		if (source === undefined) throw new Error(`Widget source ${ref.id} was not resolved`);
		hydrated[trait] = source;
	}
	return hydrated;
}

async function readPersistent(ref: BlobRef, signal?: AbortSignal): Promise<string | undefined> {
	try {
		if (!persistentStorageAvailable()) return undefined;
		return await withDeadline(
			(async () => {
				const cache = await globalThis.caches.open(CACHE_NAME);
				const response = await cache.match(cacheKey(ref.id));
				if (!response) return undefined;
				const bytes = new Uint8Array(await response.arrayBuffer());
				if (bytes.byteLength === ref.byteLength && (await blobRef(bytes)).id === ref.id)
					return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
				await cache.delete(cacheKey(ref.id));
				return undefined;
			})(),
			CACHE_IO_TIMEOUT_MS,
			signal,
		);
	} catch {
		signal?.throwIfAborted();
		return undefined;
	}
}

function persistentStorageAvailable(): boolean {
	return "caches" in globalThis && "navigator" in globalThis && "locks" in navigator;
}

async function writePersistent(ref: BlobRef, source: string): Promise<void> {
	try {
		if (!persistentStorageAvailable()) return;
		await withDeadline(
			navigator.locks.request(
				CACHE_NAME,
				{ signal: AbortSignal.timeout(CACHE_IO_TIMEOUT_MS) },
				async () => {
					try {
						const cache = await globalThis.caches.open(CACHE_NAME);
						const key = cacheKey(ref.id);
						const put = async (): Promise<void> => {
							try {
								await cache.put(key, new Response(source));
							} catch (error) {
								if (!(error instanceof DOMException) || error.name !== "QuotaExceededError")
									throw error;
								const oldest = (await cache.keys()).find((entry) =>
									entry.url.startsWith(CACHE_KEY_PREFIX),
								);
								if (!oldest || !(await cache.delete(oldest))) throw error;
								await put();
							}
						};
						await put();
					} catch {
						// Firefox reports rejected lock callbacks as page errors even when the
						// request rejection is handled. Keep optional storage failures here.
						return;
					}
				},
			),
			CACHE_IO_TIMEOUT_MS,
		);
	} catch {
		return;
	}
}

function withDeadline<T>(task: Promise<T>, milliseconds: number, signal?: AbortSignal): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		let settled = false;
		const finish = (callback: () => void): void => {
			if (settled) return;
			settled = true;
			globalThis.clearTimeout(timeout);
			signal?.removeEventListener("abort", abort);
			callback();
		};
		const abort = (): void => finish(() => reject(signal?.reason));
		const timeout = globalThis.setTimeout(
			() => finish(() => reject(new Error("Widget asset cache operation timed out"))),
			milliseconds,
		);
		signal?.addEventListener("abort", abort, { once: true });
		void task.then(
			(value) => finish(() => resolve(value)),
			(cause: unknown) => finish(() => reject(cause)),
		);
		if (signal?.aborted) abort();
	});
}

function cacheKey(assetId: string): string {
	return `${CACHE_KEY_PREFIX}${encodeURIComponent(assetId)}`;
}
