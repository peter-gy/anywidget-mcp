import type { AnyModel } from "../model";
import { isRecord, isString, type RuntimeRecord, type WidgetValue } from "../runtime-value";
import { acquireTools, toolDocument, type ToolLease, type WebMCPTool } from "./registry";
import { requestQueue } from "./request";
import { connectionSignal } from "./host";

type TraitSchema = RuntimeRecord;
export interface WebMCPDescriptor {
	id: string;
	name?: string;
	title: string;
	description: string;
	properties: Record<string, TraitSchema>;
	writable: Record<string, TraitSchema>;
}

export function exposeModel(model: AnyModel, document: Document, lifetime: AbortSignal) {
	const controller = new AbortController();
	const signal = connectionSignal(model, AbortSignal.any([lifetime, controller.signal]));
	let lease: ToolLease | undefined;
	let identity: string | undefined;
	let error: unknown;
	const target = toolDocument(document);
	const refresh = (): void => {
		if (signal.aborted) return;
		try {
			error = undefined;
			const descriptor = readDescriptor(model.get("_webmcp"));
			if (!descriptor || !target) {
				lease?.release();
				lease = undefined;
				identity = undefined;
				if (descriptor && !target)
					console.warn(
						"AnyWidget WebMCP tools require a browser with WebMCP enabled for this page.",
					);
				return;
			}
			if (identity !== descriptor.id) {
				lease?.release();
				lease = acquireTools(
					target,
					`instance:${descriptor.id}`,
					JSON.stringify(descriptor),
					(lifetime) => instanceTools(model, descriptor, lifetime),
				);
				identity = descriptor.id;
			} else
				lease?.update(JSON.stringify(descriptor), (lifetime) =>
					instanceTools(model, descriptor, lifetime),
				);
		} catch (cause) {
			error = cause;
			lease?.release();
			lease = undefined;
			identity = undefined;
			console.warn("Could not read AnyWidget WebMCP metadata.", cause);
		}
	};
	const release = (): void => {
		signal.removeEventListener("abort", release);
		controller.abort();
		model.off("change:_webmcp", refresh);
		lease?.release();
		lease = undefined;
	};
	if (!signal.aborted) {
		model.on("change:_webmcp", refresh);
		signal.addEventListener("abort", release, { once: true });
		refresh();
	}
	return {
		release,
		async ready(required = false): Promise<void> {
			signal.throwIfAborted();
			if (error) throw error;
			if (required && !lease) throw new Error("The created widget has no registered WebMCP tools.");
			await lease?.ready();
		},
	};
}

export function readDescriptor(value: WidgetValue): WebMCPDescriptor | undefined {
	if (value == null) return undefined;
	if (
		!isRecord(value) ||
		!isString(value.id) ||
		!value.id ||
		!isString(value.title) ||
		!isString(value.description)
	) {
		throw new Error("The _webmcp trait must contain widget identity and tool schemas.");
	}
	return {
		id: value.id,
		name: isString(value.name) ? value.name : undefined,
		title: value.title,
		description: value.description,
		properties: readProperties(value.properties),
		writable: readProperties(value.writable),
	};
}

function readProperties(value: WidgetValue): Record<string, TraitSchema> {
	if (!isRecord(value)) throw new Error("WebMCP trait schemas must be a property mapping.");
	const properties: Record<string, TraitSchema> = {};
	for (const [name, schema] of Object.entries(value)) {
		if (!isRecord(schema)) throw new Error(`WebMCP trait ${name} must have an object schema.`);
		Object.defineProperty(properties, name, { value: schema, enumerable: true });
	}
	return properties;
}

function instanceTools(
	model: AnyModel,
	descriptor: WebMCPDescriptor,
	signal: AbortSignal,
): WebMCPTool[] {
	const execute = requestQueue(model, signal);
	const prefix = `${descriptor.name ?? "anywidget"}_${descriptor.id}`;
	const tools: WebMCPTool[] = [
		{
			name: `${prefix}_read`,
			title: `Read ${descriptor.title}`,
			description: `${descriptor.description} Read the current Python-validated state. State schema: ${JSON.stringify({ type: "object", properties: descriptor.properties })}`,
			inputSchema: { type: "object", properties: {}, additionalProperties: false },
			annotations: { readOnlyHint: true },
			execute: (_input, options) => execute({ operation: "read" }, options?.signal),
		},
	];
	if (Object.keys(descriptor.writable).length)
		tools.push({
			name: `${prefix}_update`,
			title: `Update ${descriptor.title}`,
			description: `${descriptor.description} Update synchronized traits and return the complete state after Python validation and observers finish.`,
			inputSchema: {
				type: "object",
				properties: descriptor.writable,
				additionalProperties: false,
				minProperties: 1,
			},
			annotations: { readOnlyHint: false },
			execute: (input, options) => execute({ operation: "update", state: input }, options?.signal),
		});
	return tools;
}
