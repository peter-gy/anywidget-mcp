import { describe, expect, test, vi } from "vite-plus/test";

import { beginRuntimeReplacement } from "../src/runtime-replacement";

interface VoidDeferred {
	promise: Promise<void>;
	resolve(): void;
}

function deferred(): VoidDeferred {
	let resolve!: () => void;
	const promise = new Promise<void>((done) => {
		resolve = done;
	});
	return { promise, resolve };
}

describe("runtime replacement", () => {
	test("cancels while the current runtime is still disposing", async () => {
		const currentDisposed = deferred();
		const create = vi.fn().mockResolvedValue({});
		const disposeCancelledSession = vi.fn().mockResolvedValue(undefined);
		const replacement = beginRuntimeReplacement(
			() => currentDisposed.promise,
			create,
			disposeCancelledSession,
		);

		await Promise.resolve();
		replacement.controller.abort(new DOMException("Widget call cancelled", "AbortError"));
		currentDisposed.resolve();

		await expect(replacement.promise).rejects.toMatchObject({ name: "AbortError" });
		expect(create).not.toHaveBeenCalled();
		expect(disposeCancelledSession).toHaveBeenCalledTimes(1);
	});

	test("skips a queued result that was superseded before mounting started", async () => {
		const previous = deferred();
		const createSkipped = vi.fn().mockResolvedValue("skipped");
		const disposeSkipped = vi.fn().mockResolvedValue(undefined);
		const skippedController = new AbortController();
		const latestController = new AbortController();

		const skipped = previous.promise.then(
			() =>
				beginRuntimeReplacement(
					async () => undefined,
					createSkipped,
					disposeSkipped,
					skippedController,
				).promise,
		);
		skippedController.abort(new DOMException("Widget result superseded", "AbortError"));
		const latest = skipped
			.catch(() => undefined)
			.then(
				() =>
					beginRuntimeReplacement(
						async () => undefined,
						async () => "latest",
						async () => undefined,
						latestController,
					).promise,
			);
		previous.resolve();

		await expect(skipped).rejects.toMatchObject({ name: "AbortError" });
		await expect(latest).resolves.toBe("latest");
		expect(createSkipped).not.toHaveBeenCalled();
		expect(disposeSkipped).toHaveBeenCalledTimes(1);
	});
});
