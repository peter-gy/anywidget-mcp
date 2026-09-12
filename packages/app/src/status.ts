import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

import { isRecord, isString } from "./runtime-value";
import type { ToolArguments } from "./tool-calls";

export const DEFAULT_LOADING_MESSAGE = "Initializing widget…";

export class SessionUnavailableError extends Error {}

export class RuntimeStoppedError extends Error {
	readonly name = "WidgetRuntimeStoppedError";

	constructor(cause: unknown) {
		const detail = cause instanceof Error ? cause.message : String(cause);
		super(`Widget session stopped. ${detail}`, { cause });
	}
}

export class ReopenError extends Error {
	readonly name = "WidgetReopenError";

	constructor(
		tool: string,
		cause: unknown,
		readonly code: "reopen_unconfirmed" | "reopen_rejected" | "reopen_mount_failed",
	) {
		const detail = cause instanceof Error ? cause.message : String(cause);
		const uncertainty =
			code === "reopen_unconfirmed"
				? "\nThe request may have completed. Another attempt may create another session."
				: "";
		super(`Could not reopen ${JSON.stringify(tool)}. ${detail}${uncertainty}`, { cause });
	}
}

export function toolResultFailure(result: CallToolResult): Error {
	const message = toolResultError(result) ?? "Widget tool failed";
	const meta = result._meta?.anywidget;
	return isRecord(meta) && meta.error === "session_unavailable"
		? new SessionUnavailableError(message)
		: new Error(message);
}

const MAX_LOADING_MESSAGE_LENGTH = 120;

export function loadingMessageFromArguments(args: ToolArguments | undefined): string | undefined {
	return normalizeLoadingMessage(args?.loading_message);
}

export function loadingMessageFromPayload<Value>(payload: Value): string | undefined {
	if (!isRecord(payload)) return undefined;
	return normalizeLoadingMessage(payload.loadingMessage);
}

export function loadingMessageForTool<Value>(title: Value): string {
	const normalizedTitle = normalizeLoadingMessage(title);
	if (!normalizedTitle) return DEFAULT_LOADING_MESSAGE;
	return normalizeLoadingMessage(`Initializing ${normalizedTitle}…`) ?? DEFAULT_LOADING_MESSAGE;
}

export function toolResultError(result: CallToolResult): string | undefined {
	if (!result.isError) return undefined;
	const text = result.content
		.flatMap((item) => (item.type === "text" && item.text.trim() ? [item.text] : []))
		.join("\n");
	return text || "Widget tool failed";
}

function normalizeLoadingMessage<Value>(value: Value): string | undefined {
	if (!isString(value)) return undefined;
	const normalized = value.trim().replace(/\s+/gu, " ");
	if (
		!normalized ||
		Array.from(normalized).length > MAX_LOADING_MESSAGE_LENGTH ||
		Array.from(normalized).some(isUnsafeStatusCharacter)
	)
		return undefined;
	return normalized;
}

function isUnsafeStatusCharacter(character: string): boolean {
	const codePoint = character.codePointAt(0);
	return (
		codePoint === undefined ||
		codePoint <= 0x08 ||
		(codePoint >= 0x0b && codePoint <= 0x0c) ||
		(codePoint >= 0x0e && codePoint <= 0x1f) ||
		(codePoint >= 0x7f && codePoint <= 0x9f) ||
		(codePoint >= 0x202a && codePoint <= 0x202e) ||
		(codePoint >= 0x2066 && codePoint <= 0x2069)
	);
}
