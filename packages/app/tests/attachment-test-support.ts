import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

import { blobRef, CHUNK_BYTES, decodeChunk, encodeChunk } from "../src/attachments";
import type { RuntimeRecord } from "../src/runtime-value";
import type { ToolArguments } from "../src/tool-calls";

export async function fixtureBlob(value: string | Uint8Array<ArrayBuffer>) {
	const bytes = value instanceof Uint8Array ? value : new TextEncoder().encode(value);
	return { ref: await blobRef(bytes), bytes };
}

export function readResult(bytes: Uint8Array, args: ToolArguments): CallToolResult {
	const offset = Number(args.offset);
	return {
		content: [],
		_meta: {
			anywidget: {
				protocolVersion: 3,
				id: args.blob_id,
				byteLength: bytes.byteLength,
				offset,
				data: encodeChunk(bytes.subarray(offset, offset + CHUNK_BYTES)),
			},
		},
	};
}

export function delivery(payload: RuntimeRecord, instanceId = "session-1"): CallToolResult {
	return { content: [], _meta: { anywidget: { protocolVersion: 3, instanceId, payload } } };
}

export function writeResult(args: ToolArguments): CallToolResult {
	const received = Number(args.offset) + decodeChunk(String(args.data)).byteLength;
	return {
		content: [],
		_meta: {
			anywidget: {
				protocolVersion: 3,
				id: args.blob_id,
				byteLength: args.byte_length,
				received,
				complete: received === args.byte_length,
			},
		},
	};
}
