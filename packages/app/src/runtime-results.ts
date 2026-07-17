import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import { requireProtocolVersion } from "./assets";

export type ToolLaunch =
	| { kind: "bootstrap"; bootstrapId: string }
	| { kind: "malformed"; error: Error }
	| { kind: "other" };

export interface MaterializedLaunch {
	result: CallToolResult;
	payload: Record<string, unknown>;
}

type CallTool = (name: string, args: Record<string, unknown>) => Promise<CallToolResult>;

const BOOTSTRAP_MARKER_PREFIX = "urn:anywidget-mcp:bootstrap:";
const BOOTSTRAP_MARKER = /^urn:anywidget-mcp:bootstrap:([0-9a-f]{32})$/u;
const BOOTSTRAP_TRANSPORT_RETRY_DELAYS_MS = [100, 200] as const;

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
	operationId: string = randomOperationId(),
	signal?: AbortSignal,
): Promise<MaterializedLaunch> {
	if (launch.kind === "malformed") throw launch.error;
	if (launch.kind === "other") throw new Error("Widget tool returned no launch metadata");

	const args = {
		bootstrap_id: launch.bootstrapId,
		operation_id: operationId,
	};
	const result = await callBootstrapWithRetry(call, args, signal);
	if (result.isError) throw new Error(toolErrorText(result));
	const payload = anywidgetMeta(result._meta);
	if (!payload) throw new Error("Widget bootstrap returned no runtime data");
	requireProtocolVersion(payload.protocolVersion);
	const instanceId = nonemptyString(payload.instanceId);
	if (!instanceId) throw new Error("Widget bootstrap returned no instance ID");
	if (!nonemptyString(payload.rootModelId)) {
		throw new Error("Widget bootstrap returned no root model ID");
	}
	if (
		typeof payload.sessionIdleTimeoutMs !== "number" ||
		!Number.isFinite(payload.sessionIdleTimeoutMs) ||
		payload.sessionIdleTimeoutMs <= 0
	) {
		throw new Error("Widget bootstrap returned no valid session idle timeout");
	}
	return { result, payload };
}

async function callBootstrapWithRetry(
	call: CallTool,
	args: Record<string, unknown>,
	signal?: AbortSignal,
	attempt = 0,
): Promise<CallToolResult> {
	try {
		signal?.throwIfAborted();
		const result = call("anywidget_bootstrap", args);
		return signal ? await abortable(result, signal) : await result;
	} catch (error) {
		signal?.throwIfAborted();
		const retryDelay = BOOTSTRAP_TRANSPORT_RETRY_DELAYS_MS[attempt];
		if (retryDelay === undefined) throw error;
		await delay(retryDelay, signal);
		return callBootstrapWithRetry(call, args, signal, attempt + 1);
	}
}

export class ToolResultGate {
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

	abort(reason: unknown): void {
		this.current?.abort(reason);
	}
}

function anywidgetMeta(value: unknown): Record<string, unknown> | undefined {
	if (!isRecord(value)) return undefined;
	return isRecord(value.anywidget) ? value.anywidget : undefined;
}

function malformed(message: string): ToolLaunch {
	return { kind: "malformed", error: new Error(message) };
}

function nonemptyString(value: unknown): string | undefined {
	return typeof value === "string" && value.length > 0 ? value : undefined;
}

function toolErrorText(result: CallToolResult): string {
	const text = result.content.find((item) => item.type === "text");
	return text?.text || "Widget bootstrap failed";
}

function randomOperationId(): string {
	return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}

function abortable<T>(task: Promise<T>, signal: AbortSignal): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		let settled = false;
		const settle = (callback: () => void): void => {
			if (settled) return;
			settled = true;
			signal.removeEventListener("abort", abort);
			callback();
		};
		const abort = (): void => settle(() => reject(signal.reason));

		signal.addEventListener("abort", abort, { once: true });
		void task.then(
			(value) => settle(() => resolve(value)),
			(error: unknown) => settle(() => reject(error)),
		);
		if (signal.aborted) abort();
	});
}

function delay(milliseconds: number, signal?: AbortSignal): Promise<void> {
	if (!signal) return new Promise((resolve) => globalThis.setTimeout(resolve, milliseconds));
	signal.throwIfAborted();
	return new Promise((resolve, reject) => {
		const timeout = globalThis.setTimeout(() => {
			signal.removeEventListener("abort", abort);
			resolve();
		}, milliseconds);
		const abort = (): void => {
			globalThis.clearTimeout(timeout);
			signal.removeEventListener("abort", abort);
			reject(signal.reason);
		};
		signal.addEventListener("abort", abort, { once: true });
		if (signal.aborted) abort();
	});
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}
