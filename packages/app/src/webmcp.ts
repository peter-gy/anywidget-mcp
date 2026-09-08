import { abortable } from "./abort";
import type { WidgetDefinition } from "./binding";
import type { AnyModel } from "./model";
import {
	isCallable,
	isRecord,
	isString,
	type RuntimeRecord,
	type WidgetValue,
} from "./runtime-value";
import { loadWidget } from "./widget-definition";

type TraitSchema = RuntimeRecord;

interface WebMCPResult {
	state: RuntimeRecord;
}

export interface WebMCPDescriptor {
	id: string;
	title: string;
	description: string;
	properties: Record<string, TraitSchema>;
	writable: Record<string, TraitSchema>;
}

interface WebMCPTool {
	name: string;
	title: string;
	description: string;
	inputSchema: object;
	annotations: { readOnlyHint: boolean };
	execute(input: RuntimeRecord, options: { signal?: AbortSignal }): Promise<WebMCPResult>;
}

interface ModelContext {
	registerTool(tool: WebMCPTool, options: { signal: AbortSignal }): Promise<void>;
}

interface ToolDocument extends Document {
	modelContext?: ModelContext;
	permissionsPolicy?: { allowsFeature(name: string): boolean };
}

interface Registration {
	controller: AbortController;
	views: Set<symbol>;
}

export async function instrument(source: string, load = loadWidget): Promise<WidgetDefinition> {
	const definition = await load(source);
	const registrations = new Map<ToolDocument, Registration>();
	let initializedModel: AnyModel | undefined;
	let initializeSignal: AbortSignal | undefined;

	return {
		initialize(options) {
			initializedModel = options.model;
			initializeSignal = options.signal;
			const refresh = (): void => {
				for (const [target, registration] of registrations) {
					refreshRegistration(registration, target.modelContext, options.model);
				}
			};
			options.model.on("change:_webmcp", refresh);
			options.signal?.addEventListener(
				"abort",
				() => {
					options.model.off("change:_webmcp", refresh);
					for (const registration of registrations.values()) registration.controller.abort();
					registrations.clear();
				},
				{ once: true },
			);
			return definition.initialize?.(options);
		},
		async render(options) {
			const cleanup = await Promise.resolve(definition.render?.(options));
			let release = (): void => {};
			try {
				if (!options.signal?.aborted && !initializeSignal?.aborted) {
					const model = initializedModel ?? options.model;
					const target = toolDocument(options.el.ownerDocument);
					if (target?.modelContext) {
						let registration = registrations.get(target);
						if (!registration) {
							registration = { controller: new AbortController(), views: new Set() };
							registrations.set(target, registration);
							refreshRegistration(registration, target.modelContext, model);
						}
						const active = registration;
						const view = Symbol();
						active.views.add(view);
						release = () => {
							active.views.delete(view);
							if (active.views.size === 0) {
								active.controller.abort();
								if (registrations.get(target) === active) registrations.delete(target);
							}
						};
						options.signal?.addEventListener("abort", release, { once: true });
					} else if (model.get("_webmcp") !== null) {
						console.warn(
							"AnyWidget WebMCP tools require a browser with WebMCP enabled for this page.",
						);
					}
				}
			} catch (error) {
				console.warn("Could not read AnyWidget WebMCP metadata.", error);
			}
			let disposed = false;
			return async () => {
				if (disposed) return;
				disposed = true;
				options.signal?.removeEventListener("abort", release);
				release();
				if (isCallable(cleanup)) await Promise.resolve(cleanup());
			};
		},
	};
}

function refreshRegistration(
	registration: Registration,
	context: ModelContext | undefined,
	model: AnyModel,
): void {
	registration.controller.abort();
	try {
		const descriptor = readDescriptor(model.get("_webmcp"));
		if (descriptor && context) {
			registration.controller = registerTools(context, model, descriptor);
		}
	} catch (error) {
		console.warn("Could not read AnyWidget WebMCP metadata.", error);
	}
}

function readDescriptor(value: WidgetValue): WebMCPDescriptor | undefined {
	if (value === null) return undefined;
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

function toolDocument(document: Document): ToolDocument | undefined {
	let current: ToolDocument = document;
	if (current.permissionsPolicy?.allowsFeature("tools") === false) return undefined;
	let target = current.modelContext ? current : undefined;
	while (current.defaultView && current.defaultView.parent !== current.defaultView) {
		try {
			current = current.defaultView.parent.document;
		} catch {
			break;
		}
		if (current.permissionsPolicy?.allowsFeature("tools") === false) break;
		if (current.modelContext) target = current;
	}
	return target;
}

function registerTools(
	context: ModelContext,
	model: AnyModel,
	descriptor: WebMCPDescriptor,
): AbortController {
	const controller = new AbortController();
	let tail: Promise<WebMCPResult | undefined> = Promise.resolve(undefined);
	const execute = (
		operation: "read" | "update",
		input: RuntimeRecord,
		signal?: AbortSignal,
	): Promise<WebMCPResult> => {
		const combined = signal ? AbortSignal.any([signal, controller.signal]) : controller.signal;
		const result = tail.then(() => request(model, operation, input, combined));
		tail = result.catch(() => undefined);
		return abortable(result, combined);
	};
	const tools: WebMCPTool[] = [
		{
			name: `anywidget_${descriptor.id}_read`,
			title: `Read ${descriptor.title}`,
			description: `${descriptor.description} Read the current Python-validated state. State schema: ${JSON.stringify({ type: "object", properties: descriptor.properties })}`,
			inputSchema: { type: "object", properties: {}, additionalProperties: false },
			annotations: { readOnlyHint: true },
			execute: (input, options) => execute("read", input, options?.signal),
		},
	];
	if (Object.keys(descriptor.writable).length > 0) {
		tools.push({
			name: `anywidget_${descriptor.id}_update`,
			title: `Update ${descriptor.title}`,
			description: `${descriptor.description} Update synchronized traits and return the complete state after Python validation and observers finish.`,
			inputSchema: {
				type: "object",
				properties: descriptor.writable,
				additionalProperties: false,
				minProperties: 1,
			},
			annotations: { readOnlyHint: false },
			execute: (input, options) => execute("update", input, options?.signal),
		});
	}
	void Promise.all(
		tools.map((tool) =>
			Promise.resolve().then(() => context.registerTool(tool, { signal: controller.signal })),
		),
	).catch((error) => {
		if (controller.signal.aborted) return;
		controller.abort();
		console.warn(`Could not register WebMCP tools for ${descriptor.title}.`, error);
	});
	return controller;
}

function request(
	model: AnyModel,
	operation: "read" | "update",
	state: RuntimeRecord,
	signal: AbortSignal,
): Promise<WebMCPResult> {
	signal.throwIfAborted();
	return new Promise((resolve, reject) => {
		const id = crypto.randomUUID();
		const finish = (result: WebMCPResult | undefined, error?: WidgetValue): void => {
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
				finish({ state: message.result.state });
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
			const message: RuntimeRecord = { kind: "anywidget-webmcp", id, operation };
			if (operation === "update") message.state = state;
			model.send(message);
		} catch (error) {
			finish(
				undefined,
				error instanceof Error ? error : new Error("AnyWidget WebMCP send failed."),
			);
		}
	});
}
