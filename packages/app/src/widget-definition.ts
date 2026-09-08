import type { WidgetDefinition } from "./binding";
import { loadModule } from "./module-loader";
import { isCallable, isPlainObject } from "./runtime-value";

export async function loadWidget(esm: string, signal?: AbortSignal): Promise<WidgetDefinition> {
	signal?.throwIfAborted();
	const module = await loadModule(esm, signal);
	signal?.throwIfAborted();
	if (isCallable(module.render)) {
		return { render: module.render };
	}
	const exported = module.default;
	if (!exported) throw new Error("anywidget module must export a default definition or render");
	const definition = isCallable(exported) ? await Promise.resolve(exported()) : exported;
	return normalizeWidgetDefinition(definition);
}

function normalizeWidgetDefinition<Value>(value: Value): WidgetDefinition {
	if (!isPlainObject(value)) throw new Error("anywidget default export must return a definition");
	const initialize: unknown = Object.getOwnPropertyDescriptor(value, "initialize")?.value;
	const render: unknown = Object.getOwnPropertyDescriptor(value, "render")?.value;
	if (initialize !== undefined && !isCallable(initialize)) {
		throw new Error("anywidget initialize export must be a function");
	}
	if (render !== undefined && !isCallable(render)) {
		throw new Error("anywidget render export must be a function");
	}
	return { initialize, render };
}
