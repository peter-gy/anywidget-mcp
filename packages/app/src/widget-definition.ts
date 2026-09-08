import type { WidgetDefinition } from "./binding";
import { loadModule } from "./module-loader";
import { isCallable } from "./runtime-value";

export async function loadWidget(esm: string, signal?: AbortSignal): Promise<WidgetDefinition> {
	signal?.throwIfAborted();
	const module = await loadModule(esm, signal);
	signal?.throwIfAborted();
	if (isCallable(module.render)) {
		return normalizeWidgetDefinition({ render: module.render });
	}
	const exported = module.default;
	if (!exported) throw new Error("anywidget module must export a default definition or render");
	const definition = isCallable(exported) ? await Promise.resolve(exported()) : exported;
	return normalizeWidgetDefinition(definition);
}

function normalizeWidgetDefinition<Value>(value: Value): WidgetDefinition {
	// eslint-disable-next-line anti-slop/no-runtime-typeof -- AFM definitions are executable objects whose hooks may live on a prototype.
	if (typeof value !== "object" || value === null) {
		throw new Error("anywidget default export must return a definition");
	}
	// SAFETY: AFM definitions are objects. Lifecycle calls validate hooks when their phase starts.
	return value as WidgetDefinition;
}
