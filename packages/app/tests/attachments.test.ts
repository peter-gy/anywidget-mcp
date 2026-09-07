import { describe, expect, test, vi } from "vite-plus/test";

import { AttachmentStore, blobRef, decodeChunk } from "../src/attachments";
import type { RuntimeRecord } from "../src/runtime-value";
import type { ToolArguments } from "../src/tool-calls";
import { fixtureBlob, readResult, writeResult } from "./attachment-test-support";

describe("widget attachments", () => {
	test("reads bounded parallel chunks and retries the same immutable offset", async () => {
		const blob = await fixtureBlob(new Uint8Array(65536 * 5 + 1).fill(7));
		let retry = true;
		const call = vi.fn(async (_name, args) => {
			if (args.offset === 65536 && retry) {
				retry = false;
				throw new Error("lost response");
			}
			return readResult(blob.bytes, args);
		});
		const bytes = await new AttachmentStore("s").read(blob.ref, call);
		expect(bytes).toEqual(blob.bytes);
		expect(call.mock.calls.filter(([, args]) => args.offset === 65536)).toHaveLength(2);
		expect(call.mock.calls.every(([, args]) => args.blob_id === blob.ref.id)).toBe(true);
	});

	test.each(["truncated", "corrupt", "offset", "length", "id", "version"])(
		"rejects %s chunks before exposing attachment bytes",
		async (fault) => {
			const blob = await fixtureBlob(new Uint8Array([1, 2, 3]));
			const call = vi.fn(async () => ({
				content: [],
				_meta: {
					anywidget: {
						protocolVersion: fault === "version" ? 2 : 3,
						id: fault === "id" ? "sha256:wrong" : blob.ref.id,
						offset: fault === "offset" ? 1 : 0,
						byteLength: fault === "length" ? 4 : 3,
						data: fault === "truncated" ? "AQI=" : fault === "corrupt" ? "AQIE" : "AQID",
					},
				},
			}));
			await expect(new AttachmentStore("s").read(blob.ref, call)).rejects.toThrow();
		},
	);

	test("cancels chunk reads even when a host ignores the signal", async () => {
		const blob = await fixtureBlob(new Uint8Array(100000));
		const call = vi.fn(() => new Promise<never>(() => undefined));
		const controller = new AbortController();
		const task = new AttachmentStore("s").read(blob.ref, call, controller.signal);
		await vi.waitFor(() => expect(call).toHaveBeenCalledTimes(2));
		controller.abort();
		await expect(task).rejects.toMatchObject({ name: "AbortError" });
	});

	test("reuses a verified live buffer through an isolated copy", async () => {
		const blob = await fixtureBlob(new Uint8Array([1, 2, 3]));
		const call = vi.fn(async (_name, args) => readResult(blob.bytes, args));
		const store = new AttachmentStore("s");
		const first = await store.read(blob.ref, call);
		const second = await store.read(blob.ref, call);
		first[0] = 9;
		expect(second).toEqual(new Uint8Array([1, 2, 3]));
		expect(first.buffer).not.toBe(second.buffer);
		expect(call).toHaveBeenCalledOnce();
	});

	test("refetches bytes after a widget transfers its ArrayBuffer", async () => {
		const blob = await fixtureBlob(new Uint8Array([1, 2, 3]));
		const call = vi.fn(async (_name, args) => readResult(blob.bytes, args));
		const store = new AttachmentStore("s");
		const first = await store.read(blob.ref, call);
		structuredClone(first.buffer, { transfer: [first.buffer] });
		expect(await store.read(blob.ref, call)).toEqual(new Uint8Array([1, 2, 3]));
		expect(call).toHaveBeenCalledTimes(2);
	});

	test("refetches a live buffer after its model mutates the cached bytes", async () => {
		const blob = await fixtureBlob(new Uint8Array([1, 2, 3]));
		const call = vi.fn(async (_name, args) => readResult(blob.bytes, args));
		const store = new AttachmentStore("s");
		const first = await store.read(blob.ref, call);
		first[0] = 9;
		expect(await store.read(blob.ref, call)).toEqual(new Uint8Array([1, 2, 3]));
		expect(call).toHaveBeenCalledTimes(2);
	});

	test("uploads binary and oversized JSON using bounded frames with stable retries", async () => {
		const bytes = new Uint8Array(65536 + 1).fill(17);
		const uploads = new Map<string, Uint8Array>();
		let retry = true;
		const call = vi.fn(async (_name: string, args: ToolArguments) => {
			const id = String(args.blob_id);
			const offset = Number(args.offset);
			const chunk = decodeChunk(String(args.data));
			const target = uploads.get(id) ?? new Uint8Array(Number(args.byte_length));
			target.set(chunk, offset);
			uploads.set(id, target);
			if (offset === 65536 && retry) {
				retry = false;
				throw new Error("lost ack");
			}
			return {
				content: [],
				_meta: {
					anywidget: {
						protocolVersion: 3,
						id,
						byteLength: target.length,
						received: offset + chunk.length,
						complete: offset + chunk.length === target.length,
					},
				},
			};
		});
		const args = await new AttachmentStore("s").commArguments(
			{
				instance_id: "s",
				model_id: "m",
				operation_id: 7,
				data: { method: "update", state: { text: "λ".repeat(40000) } },
				buffers: [bytes.buffer],
			},
			call,
		);
		expect(args.operation_id).toBe(7);
		expect(call.mock.calls.every(([, value]) => value.operation_id === 7)).toBe(true);
		const binaryRef = await blobRef(bytes);
		expect(uploads.get(binaryRef.id)).toEqual(bytes);
		expect(args.payload_ref).toBeDefined();
		expect(
			call.mock.calls.filter(
				([, value]) => value.blob_id === binaryRef.id && value.offset === 65536,
			),
		).toHaveLength(2);
		expect(
			call.mock.calls.every(
				([, value]) => new TextEncoder().encode(JSON.stringify(value)).length < 128 * 1024,
			),
		).toBe(true);
	});

	test("uploads empty binary attachments with a complete acknowledgment", async () => {
		const bytes = new Uint8Array();
		const ref = await blobRef(bytes);
		const call = vi.fn(async () => ({
			content: [],
			_meta: {
				anywidget: { protocolVersion: 3, id: ref.id, byteLength: 0, received: 0, complete: true },
			},
		}));
		expect(await new AttachmentStore("s").write(bytes, 1, call)).toEqual(ref);
		expect(call).toHaveBeenCalledOnce();
	});
});

test("sends deeply nested comm JSON through an attachment even when the body is small", async () => {
	let state: RuntimeRecord = { value: 7 };
	for (let index = 0; index < 300; index += 1) state = { child: state };
	const data = { method: "update", state };
	const bytes = new TextEncoder().encode(JSON.stringify({ data, buffers: [] }));
	const ref = await blobRef(bytes);
	const call = vi.fn(async (_name: string, args: ToolArguments) => writeResult(args));
	const args = await new AttachmentStore("s").commArguments(
		{
			instance_id: "s",
			model_id: "m",
			operation_id: 1,
			acknowledged_operation_id: 0,
			data,
			buffers: [],
		},
		call,
	);
	expect(args).toEqual({
		instance_id: "s",
		model_id: "m",
		operation_id: 1,
		acknowledged_operation_id: 0,
		payload_ref: ref,
	});
	expect(call).toHaveBeenCalledOnce();
	const uploaded = decodeChunk(String(call.mock.calls[0]?.[1].data));
	expect(JSON.parse(new TextDecoder().decode(uploaded))).toEqual({ data, buffers: [] });
});
