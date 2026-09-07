import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { expect, test, vi } from "vite-plus/test";

import { decodeChunk } from "../src/attachments";
import { WidgetRuntime } from "../src/runtime";
import { ToolCallQueue, type ToolRequest } from "../src/tool-calls";
import { delivery, fixtureBlob, readResult, writeResult } from "./attachment-test-support";
import { deferred, FakeBinding, fixtureRecord, fixtureString } from "./runtime-test-support";

test("holds comm ordering and model context through binary uploads and large response hydration", async () => {
	const source = await fixtureBlob("export default {};");
	const binary = await fixtureBlob(new Uint8Array(100000).fill(23));
	const text = "λ".repeat(40000);
	const payload = await fixtureBlob(
		JSON.stringify({
			messages: [
				{
					modelId: "root",
					data: { method: "update", state: { text }, buffer_paths: [["binary"]] },
					buffers: [binary.ref],
				},
			],
			context: { version: 1, tool: "plot", state: { points: 50000 } },
		}),
	);
	const waiting = deferred<void>();
	const reading = deferred<void>();
	const comms: ToolRequest[] = [];
	const uploads = new Map<string, Uint8Array>();
	const callServerTool = vi.fn(async (request: ToolRequest): Promise<CallToolResult> => {
		const args = request.arguments ?? {};
		if (request.name === "anywidget_read") {
			if (args.blob_id === payload.ref.id && args.offset === 0) {
				reading.resolve();
				await waiting.promise;
			}
			const bytes =
				args.blob_id === source.ref.id
					? source.bytes
					: args.blob_id === payload.ref.id
						? payload.bytes
						: binary.bytes;
			return readResult(bytes, args);
		}
		if (request.name === "anywidget_write") {
			const id = String(args.blob_id);
			const chunk = decodeChunk(String(args.data));
			const bytes = uploads.get(id) ?? new Uint8Array(Number(args.byte_length));
			bytes.set(chunk, Number(args.offset));
			uploads.set(id, bytes);
			const received = Number(args.offset) + chunk.length;
			return {
				content: [],
				_meta: {
					anywidget: {
						protocolVersion: 3,
						id,
						byteLength: bytes.length,
						received,
						complete: received === bytes.length,
					},
				},
			};
		}
		if (request.name === "anywidget_comm") {
			comms.push(request);
			if (comms.length === 1)
				return {
					content: [],
					_meta: {
						anywidget: { protocolVersion: 3, instanceId: "session-1", payloadRef: payload.ref },
					},
				};
			return delivery({});
		}
		return { content: [] };
	});
	const app = {
		callServerTool,
		getHostCapabilities: () => ({ updateModelContext: { structuredContent: {} } }),
		updateModelContext: vi.fn().mockResolvedValue({}),
	};
	const runtime = await WidgetRuntime.create(
		{
			protocolVersion: 3,
			instanceId: "session-1",
			rootModelId: "root",
			models: { root: { state: {}, sourceRefs: { _esm: source.ref } } },
		},
		new ToolCallQueue(app),
		app,
		Promise.resolve(),
		(_runtime, model) => new FakeBinding(model, []),
	);
	const first = runtime.send(
		"root",
		{ method: "update", state: { text }, buffer_paths: [["binary"]] },
		[binary.bytes.buffer],
	);
	const second = runtime.send("root", { method: "custom", content: { name: "next" } }, []);
	await reading.promise;
	expect(comms).toHaveLength(1);
	expect(runtime.model("root").get("text")).toBeUndefined();
	expect(app.updateModelContext).not.toHaveBeenCalled();
	const uploadedPayload = fixtureRecord(comms[0]?.arguments?.payload_ref);
	const upload = uploads.get(fixtureString(uploadedPayload.id));
	expect(upload).toBeDefined();
	expect(JSON.parse(new TextDecoder().decode(upload))).toEqual({
		data: { method: "update", state: { text }, buffer_paths: [["binary"]] },
		buffers: [binary.ref],
	});
	waiting.resolve();
	await Promise.all([first, second]);
	expect(comms.map((request) => request.arguments?.operation_id)).toEqual([1, 2]);
	expect(comms.map((request) => request.arguments?.acknowledged_operation_id)).toEqual([0, 1]);
	expect(runtime.model("root").get("text")).toBe(text);
	expect(runtime.model("root").get("binary")).toEqual(new DataView(binary.bytes.buffer));
	await vi.waitFor(() =>
		expect(app.updateModelContext).toHaveBeenCalledWith(
			{
				structuredContent: { tool: "plot", state: { points: 50000 } },
			},
			expect.anything(),
		),
	);
	expect(
		callServerTool.mock.calls.every(
			([request]) => new TextEncoder().encode(JSON.stringify(request)).length < 128 * 1024,
		),
	).toBe(true);
	await runtime.dispose();
});

test("cancels a stalled command upload before comm dispatch and continues queued updates", async () => {
	const source = await fixtureBlob("export default {};");
	const uploading = deferred<void>();
	const calls: string[] = [];
	let cancellations = 0;
	const callServerTool = vi.fn(async (request: ToolRequest): Promise<CallToolResult> => {
		calls.push(request.name);
		if (request.name === "anywidget_read") return readResult(source.bytes, request.arguments ?? {});
		if (request.name === "anywidget_write" && request.arguments?.operation_id === 1) {
			uploading.resolve();
			return new Promise<CallToolResult>(() => undefined);
		}
		if (request.name === "anywidget_cancel") {
			cancellations += 1;
			expect(request.arguments).toEqual({
				instance_id: "session-1",
				operation_id: 1,
				acknowledged_operation_id: 0,
			});
			if (cancellations === 1) throw new Error("lost cancellation acknowledgment");
			return {
				content: [],
				_meta: {
					anywidget: { protocolVersion: 3, instanceId: "session-1", operationId: 1, retired: true },
				},
			};
		}
		if (request.name === "anywidget_write") return writeResult(request.arguments ?? {});
		if (request.name === "anywidget_comm") {
			expect(request.arguments?.operation_id).toBe(2);
			expect(request.arguments?.acknowledged_operation_id).toBe(1);
			return delivery({});
		}
		return { content: [] };
	});
	const app = {
		callServerTool,
		getHostCapabilities: () => ({}),
		updateModelContext: vi.fn().mockResolvedValue({}),
	};
	const runtime = await WidgetRuntime.create(
		{
			protocolVersion: 3,
			instanceId: "session-1",
			rootModelId: "root",
			models: { root: { state: {}, sourceRefs: { _esm: source.ref } } },
		},
		new ToolCallQueue(app),
		app,
		Promise.resolve(),
		(_runtime, model) => new FakeBinding(model, []),
	);
	const controller = new AbortController();
	const invoked = runtime
		.experimental(runtime.model("root"), controller.signal)
		.invoke(
			"upload",
			{},
			{ signal: controller.signal, buffers: [new DataView(new ArrayBuffer(100000))] },
		);
	await uploading.promise;
	controller.abort();
	await expect(invoked).rejects.toMatchObject({ name: "AbortError" });
	await runtime.send("root", { method: "update", state: { value: 1 } }, []);
	expect(calls).toEqual([
		"anywidget_read",
		"anywidget_write",
		"anywidget_cancel",
		"anywidget_cancel",
		"anywidget_write",
		"anywidget_comm",
	]);
	await runtime.dispose();
});

test("uploads and applies every binary slot in one comm operation", async () => {
	const source = await fixtureBlob("export default {};");
	const buffers = Array.from(
		{ length: 12 },
		(_, index) => new Uint8Array([index, index + 1]).buffer,
	);
	const uploads = new Map<string, Uint8Array<ArrayBuffer>>();
	const callServerTool = vi.fn(async (request: ToolRequest): Promise<CallToolResult> => {
		const args = request.arguments ?? {};
		if (request.name === "anywidget_write") {
			expect(args.operation_id).toBe(1);
			const id = String(args.blob_id);
			const bytes = decodeChunk(String(args.data));
			uploads.set(id, bytes);
			return {
				content: [],
				_meta: {
					anywidget: {
						protocolVersion: 3,
						id,
						byteLength: bytes.length,
						received: bytes.length,
						complete: true,
					},
				},
			};
		}
		if (request.name === "anywidget_read") {
			const bytes =
				args.blob_id === source.ref.id ? source.bytes : uploads.get(String(args.blob_id));
			if (!bytes) throw new Error("Unknown uploaded fixture buffer");
			return readResult(bytes, args);
		}
		if (request.name === "anywidget_comm") {
			expect(args.operation_id).toBe(1);
			const ref = fixtureRecord(args.payload_ref);
			const bytes = uploads.get(fixtureString(ref.id));
			const payload = fixtureRecord(JSON.parse(new TextDecoder().decode(bytes)));
			expect(payload.buffers).toHaveLength(12);
			return delivery({
				messages: [
					{
						modelId: "root",
						data: {
							method: "update",
							state: { data: buffers.map(() => null) },
							buffer_paths: buffers.map((_, index) => ["data", index]),
						},
						buffers: payload.buffers,
					},
				],
			});
		}
		return { content: [] };
	});
	const app = {
		callServerTool,
		getHostCapabilities: () => ({}),
		updateModelContext: vi.fn().mockResolvedValue({}),
	};
	const runtime = await WidgetRuntime.create(
		{
			protocolVersion: 3,
			instanceId: "session-1",
			rootModelId: "root",
			models: { root: { state: {}, sourceRefs: { _esm: source.ref } } },
		},
		new ToolCallQueue(app),
		app,
		Promise.resolve(),
		(_runtime, model) => new FakeBinding(model, []),
	);
	await runtime.send("root", { method: "custom", content: {} }, buffers);
	expect(runtime.model("root").get("data")).toEqual(buffers.map((buffer) => new DataView(buffer)));
	await runtime.dispose();
});
