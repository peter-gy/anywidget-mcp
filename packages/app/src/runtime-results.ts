import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { AttachmentStore, deliveryPayload } from "./attachments";
import { randomId } from "./runtime-lifecycle";
import { isNumber, isRecord, isString, type RuntimeRecord } from "./runtime-value";
import type { ToolArguments } from "./tool-calls";
import { retryTransport } from "./transport";

export type ToolLaunch =
	| { kind: "bootstrap"; bootstrapId: string }
	| { kind: "malformed"; error: Error }
	| { kind: "other" };

export interface MaterializedLaunch {
	result: CallToolResult;
	payload: RuntimeRecord;
}

type CallTool = (name: string, args: ToolArguments) => Promise<CallToolResult>;

const BOOTSTRAP_MARKER_PREFIX = "urn:anywidget-mcp:bootstrap:";
const BOOTSTRAP_MARKER = /^urn:anywidget-mcp:bootstrap:([0-9a-f]{32})$/u;

export function parseToolLaunch(result: CallToolResult): ToolLaunch {
	let bootstrapId: string | undefined;
	for (const item of result.content) {
		if (item.type !== "text" || !item.text.startsWith(BOOTSTRAP_MARKER_PREFIX)) continue;
		const match = BOOTSTRAP_MARKER.exec(item.text);
		if (!match) return malformed("Widget launch has an invalid bootstrap marker");
		const candidate = match[1]!;
		if (bootstrapId !== undefined && candidate !== bootstrapId) {
			return malformed("Widget launch has conflicting bootstrap markers");
		}
		bootstrapId = candidate;
	}
	if (bootstrapId === undefined) return { kind: "other" };

	return { kind: "bootstrap", bootstrapId };
}

export async function loadWidgetRuntime(
	launch: ToolLaunch,
	call: CallTool,
	operationId: string = randomId(),
	signal?: AbortSignal,
): Promise<MaterializedLaunch> {
	if (launch.kind === "malformed") throw launch.error;
	if (launch.kind === "other") throw new Error("Widget tool returned no launch metadata");

	const args = {
		bootstrap_id: launch.bootstrapId,
		operation_id: operationId,
	};
	const result = await retryTransport(() => call("anywidget_bootstrap", args), signal);
	if (result.isError) throw new Error(toolErrorText(result));
	const delivery = anywidgetMeta(result._meta);
	if (!delivery) throw new Error("Widget bootstrap returned no runtime data");
	const instanceId = nonemptyString(delivery.instanceId);
	if (!instanceId) throw new Error("Widget bootstrap returned no instance ID");
	const payload = await deliveryPayload(result, new AttachmentStore(instanceId), call, signal);
	if (!nonemptyString(payload.rootModelId)) {
		throw new Error("Widget bootstrap returned no root model ID");
	}
	if (
		payload.sessionIdleTimeoutMs !== undefined &&
		(!isNumber(payload.sessionIdleTimeoutMs) ||
			!Number.isFinite(payload.sessionIdleTimeoutMs) ||
			payload.sessionIdleTimeoutMs <= 0)
	) {
		throw new Error("Widget bootstrap returned no valid session idle timeout");
	}
	return { result, payload };
}

export class ToolResultGate {
	// Retain accepted bootstrap IDs for the app lifetime so a replay cannot replace
	// a pending, mounted, or newer runtime.
	private readonly retained = new Set<string>();
	private current?: AbortController;
	private launchAccepted = false;

	get acceptedLaunch(): boolean {
		return this.launchAccepted;
	}

	deliver(bootstrapId: string, schedule: (controller: AbortController) => void): boolean {
		if (this.retained.has(bootstrapId)) return false;
		this.retained.add(bootstrapId);
		this.launchAccepted = true;
		this.current?.abort(new DOMException("Widget result superseded", "AbortError"));
		const controller = new AbortController();
		this.current = controller;
		schedule(controller);
		return true;
	}

	abort(cause: unknown): void {
		this.current?.abort(cause);
	}
}

function anywidgetMeta<Value>(value: Value): RuntimeRecord | undefined {
	if (!isRecord(value)) return undefined;
	return isRecord(value.anywidget) ? value.anywidget : undefined;
}

function malformed(message: string): ToolLaunch {
	return { kind: "malformed", error: new Error(message) };
}

function nonemptyString<Value>(value: Value): string | undefined {
	return isString(value) && value.length > 0 ? value : undefined;
}

function toolErrorText(result: CallToolResult): string {
	const text = result.content.find((item) => item.type === "text");
	return text?.text || "Widget bootstrap failed";
}
