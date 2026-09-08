import { abortable } from "../abort";
import type { AnyModel } from "../model";
import { isRecord, isString, type RuntimeRecord, type WidgetValue } from "../runtime-value";

export function requestQueue(model: AnyModel, lifetime: AbortSignal) {
	let tail: Promise<RuntimeRecord | undefined> = Promise.resolve(undefined);
	return (message: RuntimeRecord, signal?: AbortSignal): Promise<RuntimeRecord> => {
		const combined = signal ? AbortSignal.any([signal, lifetime]) : lifetime;
		const result = tail.then(() => request(model, message, combined));
		tail = result.catch(() => undefined);
		return abortable(result, combined);
	};
}

export function request(
	model: AnyModel,
	message: RuntimeRecord,
	signal: AbortSignal,
): Promise<RuntimeRecord> {
	signal.throwIfAborted();
	return new Promise((resolve, reject) => {
		const id = crypto.randomUUID();
		const finish = (result: RuntimeRecord | undefined, error?: WidgetValue): void => {
			clearTimeout(timeout);
			signal.removeEventListener("abort", abort);
			model.off("msg:custom", receive);
			if (error !== undefined) reject(error);
			else if (result) resolve(result);
		};
		const abort = (): void => finish(undefined, signal.reason);
		const receive = (message: WidgetValue): void => {
			if (!isRecord(message) || message.kind !== "anywidget-webmcp-result" || message.id !== id)
				return;
			if (isString(message.error)) finish(undefined, new Error(message.error));
			else if (isRecord(message.result) && isRecord(message.result.state)) {
				finish(message.result);
			} else finish(undefined, new Error("Python returned an invalid AnyWidget WebMCP result."));
		};
		const timeout = setTimeout(
			() =>
				finish(undefined, new Error("AnyWidget WebMCP operation timed out waiting for Python.")),
			10_000,
		);
		signal.addEventListener("abort", abort, { once: true });
		model.on("msg:custom", receive);
		try {
			model.send({ ...message, kind: "anywidget-webmcp", id });
		} catch (error) {
			finish(
				undefined,
				error instanceof Error ? error : new Error("AnyWidget WebMCP send failed."),
			);
		}
	});
}
