export interface RuntimeReplacement<T> {
	controller: AbortController;
	promise: Promise<T>;
}

export function beginRuntimeReplacement<T>(
	disposeCurrent: (signal: AbortSignal) => Promise<void>,
	create: (signal: AbortSignal) => Promise<T>,
	disposeUnstarted: () => Promise<void>,
	controller = new AbortController(),
): RuntimeReplacement<T> {
	// Cleanup ownership transfers to create when it starts. Before that point this
	// wrapper disposes the unstarted server session after cancellation or failure.
	let creationStarted = false;
	const promise = Promise.resolve().then(async () => {
		try {
			await disposeCurrent(controller.signal);
			controller.signal.throwIfAborted();
			creationStarted = true;
			return await create(controller.signal);
		} catch (error) {
			if (!creationStarted) await disposeUnstarted();
			throw error;
		}
	});
	return { controller, promise };
}
