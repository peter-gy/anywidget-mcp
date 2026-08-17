const TRANSPORT_RETRY_DELAYS_MS = [100, 200] as const;

export function retryTransport<T>(request: () => Promise<T>, signal?: AbortSignal): Promise<T> {
	// Each attempt invokes the same closure. Callers must keep request arguments
	// and replay identity stable across retries.
	return retryTransportAttempt(request, signal, 0);
}

async function retryTransportAttempt<T>(
	request: () => Promise<T>,
	signal: AbortSignal | undefined,
	attempt: number,
): Promise<T> {
	try {
		signal?.throwIfAborted();
		const result = request();
		return signal ? await abortable(result, signal) : await result;
	} catch (error) {
		signal?.throwIfAborted();
		const retryDelay = TRANSPORT_RETRY_DELAYS_MS[attempt];
		if (retryDelay === undefined) throw error;
		await delay(retryDelay, signal);
		return retryTransportAttempt(request, signal, attempt + 1);
	}
}

function abortable<T>(task: Promise<T>, signal: AbortSignal): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		let settled = false;
		const finish = (callback: () => void): void => {
			if (settled) return;
			settled = true;
			signal.removeEventListener("abort", abort);
			callback();
		};
		const abort = (): void => finish(() => reject(signal.reason));

		if (signal.aborted) abort();
		else signal.addEventListener("abort", abort, { once: true });
		void task.then(
			(value) => finish(() => resolve(value)),
			(cause: unknown) => finish(() => reject(cause)),
		);
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
