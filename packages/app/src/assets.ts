import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

import type { QueuedToolCall } from "./tool-calls";

export const PROTOCOL_VERSION = 1;

export type AssetKind = "esm" | "css";
export type SourceTrait = "_esm" | "_css";
export type SourceRefs = Partial<Record<SourceTrait, string>>;

interface AssetDescriptor {
	kind: AssetKind;
	byteLength: number;
}

interface AssetContent extends AssetDescriptor {
	text: string;
}

const ASSET_ID = /^(esm|css):sha256:([0-9a-f]{64})$/;
const SOURCE_KIND: Record<SourceTrait, AssetKind> = { _esm: "esm", _css: "css" };
const MAX_ASSETS_PER_REQUEST = 128;
const CACHE_IO_TIMEOUT_MS = 500;
const CACHE_NAME = "anywidget-mcp-assets-v1";
const CACHE_KEY_PREFIX = "https://anywidget-mcp.invalid/assets/";
const MEMORY_CACHE_MAX_BYTES = 32 * 1024 * 1024;

interface MemoryAsset {
	source: string;
	byteLength: number;
}

export class AssetMemoryCache {
	private readonly entries = new Map<string, MemoryAsset>();
	private byteLength = 0;

	constructor(private readonly maxByteLength: number) {
		if (!Number.isSafeInteger(maxByteLength) || maxByteLength < 0) {
			throw new TypeError("Asset memory cache size must be a non-negative safe integer");
		}
	}

	get(assetId: string): string | undefined {
		const entry = this.entries.get(assetId);
		if (!entry) return undefined;
		this.entries.delete(assetId);
		this.entries.set(assetId, entry);
		return entry.source;
	}

	set(assetId: string, source: string, byteLength: number): void {
		this.delete(assetId);
		if (byteLength > this.maxByteLength) return;
		this.entries.set(assetId, { source, byteLength });
		this.byteLength += byteLength;
		while (this.byteLength > this.maxByteLength) {
			const oldest = this.entries.keys().next().value;
			if (oldest === undefined) break;
			this.delete(oldest);
		}
	}

	delete(assetId: string): void {
		const entry = this.entries.get(assetId);
		if (!entry) return;
		this.entries.delete(assetId);
		this.byteLength -= entry.byteLength;
	}

	clear(): void {
		this.entries.clear();
		this.byteLength = 0;
	}
}

const memoryCache = new AssetMemoryCache(MEMORY_CACHE_MAX_BYTES);

export class AssetStore {
	constructor(private readonly instanceId: string) {}

	async resolve(
		manifestValue: unknown,
		refValues: unknown[],
		call: QueuedToolCall,
		signal?: AbortSignal,
	): Promise<Map<string, string>> {
		signal?.throwIfAborted();
		const manifest = normalizeManifest(manifestValue);
		const references = new Set<string>();
		for (const value of refValues) {
			for (const [trait, assetId] of Object.entries(normalizeSourceRefs(value))) {
				const descriptor = manifest.get(assetId);
				if (!descriptor) throw new Error(`Missing manifest entry for widget asset ${assetId}`);
				if (descriptor.kind !== SOURCE_KIND[trait as SourceTrait]) {
					throw new Error(`Widget asset ${assetId} has the wrong source kind for ${trait}`);
				}
				references.add(assetId);
			}
		}
		for (const assetId of manifest.keys()) {
			if (!references.has(assetId)) {
				throw new Error(`Widget asset manifest contains unreferenced asset ${assetId}`);
			}
		}

		const resolved = new Map<string, string>();
		const missing: string[] = [];
		const cached = await Promise.all(
			Array.from(references, async (assetId): Promise<[string, string | undefined]> => {
				const descriptor = manifest.get(assetId);
				if (!descriptor) throw new Error(`Missing manifest entry for widget asset ${assetId}`);
				const memory = memoryCache.get(assetId);
				if (memory !== undefined && (await verifyAsset(assetId, descriptor, memory))) {
					return [assetId, memory];
				}
				memoryCache.delete(assetId);

				const persistent = await readPersistent(assetId, descriptor, signal);
				if (persistent !== undefined) {
					memoryCache.set(assetId, persistent, descriptor.byteLength);
					return [assetId, persistent];
				}
				return [assetId, undefined];
			}),
		);
		for (const [assetId, source] of cached) {
			if (source === undefined) missing.push(assetId);
			else resolved.set(assetId, source);
		}
		signal?.throwIfAborted();

		if (missing.length > 0) {
			const batches = Array.from(
				{ length: Math.ceil(missing.length / MAX_ASSETS_PER_REQUEST) },
				(_, index) =>
					missing.slice(index * MAX_ASSETS_PER_REQUEST, (index + 1) * MAX_ASSETS_PER_REQUEST),
			);
			const fetchedEntries = await batches.reduce<Promise<Array<[string, string]>>>(
				async (previous, batch) => [
					...(await previous),
					...Array.from(await this.fetch(batch, manifest, call, signal)),
				],
				Promise.resolve([]),
			);
			const fetched = new Map(fetchedEntries);
			for (const [assetId, source] of fetched) {
				const descriptor = manifest.get(assetId);
				if (!descriptor) throw new Error(`Missing manifest entry for widget asset ${assetId}`);
				memoryCache.set(assetId, source, descriptor.byteLength);
				resolved.set(assetId, source);
			}
			void Promise.all(
				Array.from(fetched, ([assetId, source]) => writePersistent(assetId, source)),
			);
		}
		signal?.throwIfAborted();
		return resolved;
	}

	private async fetch(
		assetIds: string[],
		manifest: Map<string, AssetDescriptor>,
		call: QueuedToolCall,
		signal?: AbortSignal,
	): Promise<Map<string, string>> {
		signal?.throwIfAborted();
		const args = {
			instance_id: this.instanceId,
			asset_ids: assetIds,
		};
		const request = signal
			? call("anywidget_assets", args, signal)
			: call("anywidget_assets", args);
		const result = signal ? await abortable(request, signal) : await request;
		signal?.throwIfAborted();
		if (result.isError) throw new Error(toolErrorText(result));
		const meta = isRecord(result._meta) ? result._meta.anywidget : undefined;
		if (!isRecord(meta) || meta.protocolVersion !== PROTOCOL_VERSION) {
			throw new Error("Widget asset response uses an incompatible protocol version");
		}
		const assetContents = meta.assetContents;
		if (!isRecord(assetContents)) {
			throw new Error("Widget asset response has no asset contents");
		}

		const expected = new Set(assetIds);
		const returned = Object.keys(assetContents);
		for (const assetId of returned) {
			if (!expected.has(assetId)) {
				throw new Error(`Widget asset response contains unexpected asset ${assetId}`);
			}
		}
		const entries = await Promise.all(
			assetIds.map(async (assetId): Promise<[string, string]> => {
				const value = assetContents[assetId];
				const descriptor = manifest.get(assetId);
				if (!descriptor || !isRecord(value)) {
					throw new Error(`Widget asset response is missing ${assetId}`);
				}
				const content = normalizeContent(assetId, value);
				if (
					content.kind !== descriptor.kind ||
					content.byteLength !== descriptor.byteLength ||
					!(await verifyAsset(assetId, descriptor, content.text))
				) {
					throw new Error(`Widget asset response failed verification for ${assetId}`);
				}
				return [assetId, content.text];
			}),
		);
		return new Map(entries);
	}
}

export function requireProtocolVersion(value: unknown): void {
	if (value !== PROTOCOL_VERSION) {
		throw new Error(
			`Widget payload protocol version ${String(value)} is incompatible with version ${PROTOCOL_VERSION}`,
		);
	}
}

export function normalizeSourceRefs(value: unknown): SourceRefs {
	if (value === undefined) return {};
	if (!isRecord(value)) throw new Error("Widget source references must be an object");
	const refs: SourceRefs = {};
	for (const [trait, assetId] of Object.entries(value)) {
		if (trait !== "_esm" && trait !== "_css") {
			throw new Error(`Unknown widget source reference ${trait}`);
		}
		if (typeof assetId !== "string" || !ASSET_ID.test(assetId)) {
			throw new Error(`Invalid widget source reference for ${trait}`);
		}
		refs[trait] = assetId;
	}
	return refs;
}

export function hydrateSources(
	state: Record<string, unknown>,
	refValue: unknown,
	assets: Map<string, string>,
): Record<string, unknown> {
	for (const trait of ["_esm", "_css"] as const) {
		if (Object.hasOwn(state, trait)) {
			throw new Error(`Widget source ${trait} must use a content-addressed reference`);
		}
	}
	const hydrated = { ...state };
	for (const [trait, assetId] of Object.entries(normalizeSourceRefs(refValue))) {
		const source = assets.get(assetId);
		if (source === undefined) throw new Error(`Widget asset ${assetId} was not resolved`);
		hydrated[trait] = source;
	}
	return hydrated;
}

export async function widgetAssetId(kind: AssetKind, source: string): Promise<string> {
	const bytes = new TextEncoder().encode(`anywidget-mcp-asset-v1\0${kind}\0${source}`);
	const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
	const hex = Array.from(new Uint8Array(digest), (value) =>
		value.toString(16).padStart(2, "0"),
	).join("");
	return `${kind}:sha256:${hex}`;
}

export function clearAssetMemoryCache(): void {
	memoryCache.clear();
}

function normalizeManifest(value: unknown): Map<string, AssetDescriptor> {
	if (!isRecord(value)) throw new Error("Widget payload has no asset manifest");
	const manifest = new Map<string, AssetDescriptor>();
	for (const [assetId, raw] of Object.entries(value)) {
		const match = ASSET_ID.exec(assetId);
		if (!match || !isRecord(raw)) throw new Error(`Invalid widget asset manifest entry ${assetId}`);
		const kind = raw.kind;
		const byteLength = raw.byteLength;
		if (
			(kind !== "esm" && kind !== "css") ||
			match[1] !== kind ||
			typeof byteLength !== "number" ||
			!Number.isSafeInteger(byteLength) ||
			byteLength < 0
		) {
			throw new Error(`Invalid widget asset manifest entry ${assetId}`);
		}
		manifest.set(assetId, { kind, byteLength });
	}
	return manifest;
}

function normalizeContent(assetId: string, value: Record<string, unknown>): AssetContent {
	const kind = value.kind;
	const byteLength = value.byteLength;
	const text = value.text;
	if (
		(kind !== "esm" && kind !== "css") ||
		typeof byteLength !== "number" ||
		!Number.isSafeInteger(byteLength) ||
		byteLength < 0 ||
		typeof text !== "string"
	) {
		throw new Error(`Invalid widget asset content ${assetId}`);
	}
	return { kind, byteLength, text };
}

async function verifyAsset(
	assetId: string,
	descriptor: AssetDescriptor,
	source: string,
): Promise<boolean> {
	if (new TextEncoder().encode(source).byteLength !== descriptor.byteLength) return false;
	return (await widgetAssetId(descriptor.kind, source)) === assetId;
}

async function readPersistent(
	assetId: string,
	descriptor: AssetDescriptor,
	signal?: AbortSignal,
): Promise<string | undefined> {
	try {
		if (!("caches" in globalThis)) return undefined;
		return await withDeadline(
			(async () => {
				const cache = await globalThis.caches.open(CACHE_NAME);
				const response = await cache.match(cacheKey(assetId));
				if (!response) return undefined;
				const source = await response.text();
				if (await verifyAsset(assetId, descriptor, source)) return source;
				await cache.delete(cacheKey(assetId));
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

async function writePersistent(assetId: string, source: string): Promise<void> {
	try {
		if (!("caches" in globalThis)) return;
		await withDeadline(
			(async () => {
				const cache = await globalThis.caches.open(CACHE_NAME);
				await cache.put(cacheKey(assetId), new Response(source));
			})(),
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
			(error: unknown) => finish(() => reject(error)),
		);
		if (signal?.aborted) abort();
	});
}

function abortable<T>(task: Promise<T>, signal: AbortSignal): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		let settled = false;
		const finish = (callback: () => void): void => {
			if (settled) return;
			settled = true;
			signal.removeEventListener("abort", abort);
			callback();
		};
		const abort = (): void => finish(() => reject(signal.reason));
		signal.addEventListener("abort", abort, { once: true });
		void task.then(
			(value) => finish(() => resolve(value)),
			(error: unknown) => finish(() => reject(error)),
		);
		if (signal.aborted) abort();
	});
}

function cacheKey(assetId: string): string {
	return `${CACHE_KEY_PREFIX}${encodeURIComponent(assetId)}`;
}

function toolErrorText(result: CallToolResult): string {
	const text = result.content.find((item) => item.type === "text");
	return text?.text ?? "Widget asset request failed";
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}
