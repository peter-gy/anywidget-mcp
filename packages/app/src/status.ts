import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

import { isRecord, isString } from "./runtime-value";
import type { ToolArguments } from "./tool-calls";

export const DEFAULT_LOADING_MESSAGE = "Initializing widget…";

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
	const text = result.content.find((item) => item.type === "text");
	return text?.text || "Widget tool failed";
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
