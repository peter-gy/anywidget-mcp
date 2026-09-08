export function abortable<T>(task: Promise<T>, signal: AbortSignal): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		let settled = false;
		const finish = (callback: () => void): void => {
			if (settled) return;
			settled = true;
			signal.removeEventListener("abort", abort);
			callback();
		};
		const abort = (): void =>
			finish(() =>
				reject(signal.reason ?? new DOMException("Widget runtime is closed", "AbortError")),
			);

		if (signal.aborted) {
			abort();
		} else {
			signal.addEventListener("abort", abort, { once: true });
		}
		void task.then(
			(value) => finish(() => resolve(value)),
			(cause: unknown) => finish(() => reject(cause)),
		);
	});
}
