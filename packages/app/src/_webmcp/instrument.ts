import type { WidgetDefinition } from "../binding";
import type { AnyModel } from "../model";
import { isCallable } from "../runtime-value";
import { loadWidget } from "../widget-definition";
import { exposeModel } from "./instance";

export async function instrument(source: string, load = loadWidget): Promise<WidgetDefinition> {
	const definition = await load(source);
	let initializedModel: AnyModel | undefined;
	let initializeSignal: AbortSignal | undefined;
	return {
		initialize(options) {
			initializedModel = options.model;
			initializeSignal = options.signal;
			return definition.initialize?.(options);
		},
		async render(options) {
			const cleanup = await Promise.resolve(definition.render?.(options));
			const signal = initializeSignal
				? AbortSignal.any([options.signal, initializeSignal])
				: options.signal;
			const exposure = exposeModel(
				initializedModel ?? options.model,
				options.el.ownerDocument,
				signal,
			);
			void exposure.ready().catch(() => undefined);
			let disposed = false;
			return async () => {
				if (disposed) return;
				disposed = true;
				exposure.release();
				if (isCallable(cleanup)) await Promise.resolve(cleanup());
			};
		},
	};
}
