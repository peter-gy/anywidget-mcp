import { afterEach, beforeEach, describe, expect, test, vi } from "vite-plus/test";

import { ModelContextSync, type ContextApp, type ModelContextSnapshot } from "../src/context";
import { isNumber, isRecord } from "../src/runtime-value";

type UpdateParams = Parameters<ContextApp["updateModelContext"]>[0];

function snapshot(version: number): ModelContextSnapshot {
	return {
		version,
		tool: "example.Counter",
		state: { value: version },
	};
}

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

function contextValue(params: UpdateParams): number | undefined {
	const structured = params.structuredContent;
	if (!isRecord(structured) || !isRecord(structured.state)) return undefined;
	return isNumber(structured.state.value) ? structured.state.value : undefined;
}

describe("ModelContextSync", () => {
	beforeEach(() => vi.useFakeTimers());
	afterEach(() => {
		vi.useRealTimers();
		vi.restoreAllMocks();
	});

	test("sends structured state when the host advertises that modality", async () => {
		const updateModelContext = vi.fn().mockResolvedValue({});
		const app = {
			getHostCapabilities: () => ({ updateModelContext: { structuredContent: {} } }),
			updateModelContext,
		};
		const sync = new ModelContextSync(app, Promise.resolve(), vi.fn(), 10);

		sync.enqueue(snapshot(1));
		await vi.runAllTimersAsync();

		expect(updateModelContext).toHaveBeenCalledWith(
			{ structuredContent: { tool: "example.Counter", state: { value: 1 } } },
			{ signal: expect.any(AbortSignal) },
		);
		await sync.dispose();
	});

	test("uses concise text when structured context is unavailable", async () => {
		const updateModelContext = vi.fn().mockResolvedValue({});
		const app = {
			getHostCapabilities: () => ({ updateModelContext: { text: {} } }),
			updateModelContext,
		};
		const sync = new ModelContextSync(app, Promise.resolve(), vi.fn(), 10);

		sync.enqueue(snapshot(1));
		await vi.runAllTimersAsync();

		expect(updateModelContext).toHaveBeenCalledWith(
			{
				content: [{ type: "text", text: 'Current example.Counter state: {"value":1}' }],
			},
			{ signal: expect.any(AbortSignal) },
		);
		await sync.dispose();
	});

	test("skips context delivery when the host does not advertise it", async () => {
		const updateModelContext = vi.fn().mockResolvedValue({});
		const app = {
			getHostCapabilities: () => ({}),
			updateModelContext,
		};
		const sync = new ModelContextSync(app, Promise.resolve(), vi.fn(), 10);

		sync.enqueue(snapshot(1));
		await vi.runAllTimersAsync();

		expect(updateModelContext).not.toHaveBeenCalled();
		await sync.dispose();
	});

	test("retains one latest snapshot behind an in-flight update", async () => {
		const first = deferred();
		const calls: UpdateParams[] = [];
		const app = {
			getHostCapabilities: () => ({ updateModelContext: { structuredContent: {} } }),
			async updateModelContext(params: UpdateParams) {
				calls.push(params);
				if (calls.length === 1) await first.promise;
				return {};
			},
		};
		const sync = new ModelContextSync(app, Promise.resolve(), vi.fn(), 10);

		sync.enqueue(snapshot(1));
		await vi.advanceTimersByTimeAsync(10);
		sync.enqueue(snapshot(2));
		sync.enqueue(snapshot(3));

		expect(calls).toEqual([
			{ structuredContent: { tool: "example.Counter", state: { value: 1 } } },
		]);

		first.resolve();
		await Promise.resolve();
		await vi.runAllTimersAsync();

		expect(calls).toEqual([
			{ structuredContent: { tool: "example.Counter", state: { value: 1 } } },
			{ structuredContent: { tool: "example.Counter", state: { value: 3 } } },
		]);
		await sync.dispose();
	});

	test("retries a rejected latest snapshot while the widget is idle", async () => {
		const reportError = vi.fn();
		const updateModelContext = vi
			.fn()
			.mockRejectedValueOnce(new Error("denied"))
			.mockResolvedValue({});
		const app = {
			getHostCapabilities: () => ({ updateModelContext: { structuredContent: {} } }),
			updateModelContext,
		};
		const sync = new ModelContextSync(app, Promise.resolve(), reportError, 10);

		sync.enqueue(snapshot(1));
		await vi.runAllTimersAsync();

		expect(updateModelContext).toHaveBeenCalledTimes(2);
		expect(reportError).toHaveBeenCalledWith(expect.objectContaining({ message: "denied" }));
		await sync.dispose();
	});

	test("bounds retries when the host keeps rejecting context", async () => {
		const reportError = vi.fn();
		const updateModelContext = vi.fn().mockRejectedValue(new Error("denied"));
		const app = {
			getHostCapabilities: () => ({ updateModelContext: { structuredContent: {} } }),
			updateModelContext,
		};
		const sync = new ModelContextSync(app, Promise.resolve(), reportError, 10);

		sync.enqueue(snapshot(1));
		await vi.runAllTimersAsync();

		expect(updateModelContext).toHaveBeenCalledTimes(3);
		expect(reportError).toHaveBeenCalledTimes(3);
		await sync.dispose();
	});

	test("restores the latest snapshot after an older timed-out update settles", async () => {
		const first = deferred();
		const calls: number[] = [];
		const reportError = vi.fn();
		let visible = 0;
		const app = {
			getHostCapabilities: () => ({ updateModelContext: { structuredContent: {} } }),
			updateModelContext(params: UpdateParams) {
				const value = contextValue(params) ?? 0;
				calls.push(value);
				if (calls.length === 1) {
					return first.promise.then(() => {
						visible = value;
						return {};
					});
				}
				visible = value;
				return Promise.resolve({});
			},
		};
		const sync = new ModelContextSync(app, Promise.resolve(), reportError, 10, 25);

		sync.enqueue(snapshot(1));
		await vi.advanceTimersByTimeAsync(10);
		sync.enqueue(snapshot(2));
		await vi.advanceTimersByTimeAsync(35);
		expect(visible).toBe(2);
		expect(reportError).toHaveBeenCalledWith(
			expect.objectContaining({ message: "Model context update timed out" }),
		);

		first.resolve();
		await Promise.resolve();
		await vi.runAllTimersAsync();

		expect(calls).toEqual([1, 2, 2]);
		expect(visible).toBe(2);
		await sync.dispose();
	});

	test("reports readiness failure before context delivery", async () => {
		const reportError = vi.fn();
		const app = {
			getHostCapabilities: () => ({ updateModelContext: { structuredContent: {} } }),
			updateModelContext: vi.fn().mockResolvedValue({}),
		};
		const sync = new ModelContextSync(
			app,
			Promise.reject(new Error("connection failed")),
			reportError,
			10,
		);

		await Promise.resolve();
		sync.enqueue(snapshot(1));
		await vi.runAllTimersAsync();

		expect(app.updateModelContext).not.toHaveBeenCalled();
		expect(reportError).toHaveBeenCalledWith(
			expect.objectContaining({ message: "connection failed" }),
		);
		await sync.dispose();
	});

	test("coalesces snapshots queued before host readiness", async () => {
		const ready = deferred();
		const app = {
			getHostCapabilities: () => ({ updateModelContext: { structuredContent: {} } }),
			updateModelContext: vi.fn().mockResolvedValue({}),
		};
		const sync = new ModelContextSync(app, ready.promise, vi.fn(), 10);

		sync.enqueue(snapshot(1));
		sync.enqueue(snapshot(2));
		await vi.advanceTimersByTimeAsync(10);

		ready.resolve();
		await vi.runAllTimersAsync();

		expect(app.updateModelContext).toHaveBeenCalledOnce();
		expect(app.updateModelContext).toHaveBeenCalledWith(
			{ structuredContent: { tool: "example.Counter", state: { value: 2 } } },
			{ signal: expect.any(AbortSignal) },
		);
		await sync.dispose();
	});

	test("joins an old host update before a replacement context starts", async () => {
		const oldRequest = deferred();
		const events: string[] = [];
		const app = {
			getHostCapabilities: () => ({ updateModelContext: { structuredContent: {} } }),
			updateModelContext(params: UpdateParams) {
				const value = contextValue(params);
				events.push(`start:${value}`);
				if (value !== 1) return Promise.resolve({});
				return oldRequest.promise.then(() => {
					events.push("finish:1");
					return {};
				});
			},
		};
		const oldSync = new ModelContextSync(app, Promise.resolve(), vi.fn(), 10, 1000);
		oldSync.enqueue(snapshot(1));
		await vi.advanceTimersByTimeAsync(10);

		const oldDisposal = oldSync.dispose();
		expect(oldSync.dispose()).toBe(oldDisposal);
		let disposed = false;
		void oldDisposal.then(() => {
			disposed = true;
		});
		await Promise.resolve();
		expect(disposed).toBe(false);

		oldRequest.resolve();
		await oldDisposal;
		const replacement = new ModelContextSync(app, Promise.resolve(), vi.fn(), 10, 1000);
		replacement.enqueue(snapshot(2));
		await vi.advanceTimersByTimeAsync(10);

		expect(events).toEqual(["start:1", "finish:1", "start:2"]);
		await replacement.dispose();
	});

	test("republishes replacement context after an old host update settles late", async () => {
		const oldRequest = deferred();
		const events: string[] = [];
		let visible = 0;
		const app = {
			getHostCapabilities: () => ({ updateModelContext: { structuredContent: {} } }),
			updateModelContext(params: UpdateParams) {
				const value = contextValue(params) ?? 0;
				events.push(`start:${value}`);
				if (value === 1) {
					return oldRequest.promise.then(() => {
						events.push("finish:1");
						visible = 1;
						return {};
					});
				}
				events.push("finish:2");
				visible = 2;
				return Promise.resolve({});
			},
		};
		const oldSync = new ModelContextSync(app, Promise.resolve(), vi.fn(), 10, 10);
		oldSync.enqueue(snapshot(1));
		await vi.advanceTimersByTimeAsync(10);

		const oldDisposal = oldSync.dispose();
		await vi.advanceTimersByTimeAsync(10);
		await oldDisposal;
		const replacement = new ModelContextSync(app, Promise.resolve(), vi.fn(), 10, 10);
		replacement.enqueue(snapshot(2));
		await vi.advanceTimersByTimeAsync(10);

		expect(events).toEqual(["start:1", "start:2", "finish:2"]);
		expect(visible).toBe(2);
		oldRequest.resolve();
		await Promise.resolve();
		expect(events).toEqual(["start:1", "start:2", "finish:2", "finish:1"]);
		expect(visible).toBe(1);

		await vi.runAllTimersAsync();

		expect(events).toEqual(["start:1", "start:2", "finish:2", "finish:1", "start:2", "finish:2"]);
		expect(visible).toBe(2);
		await replacement.dispose();
	});

	test("disposes while an in-flight host update ignores cancellation", async () => {
		const updateModelContext = vi.fn(() => new Promise<never>(() => undefined));
		const app = {
			getHostCapabilities: () => ({ updateModelContext: { structuredContent: {} } }),
			updateModelContext,
		};
		const sync = new ModelContextSync(app, Promise.resolve(), vi.fn(), 10, 25);

		sync.enqueue(snapshot(1));
		await vi.advanceTimersByTimeAsync(10);
		expect(updateModelContext).toHaveBeenCalledTimes(1);

		const disposal = sync.dispose();
		let disposed = false;
		void disposal.then(() => {
			disposed = true;
		});
		await Promise.resolve();
		expect(disposed).toBe(false);

		await vi.advanceTimersByTimeAsync(25);
		await expect(disposal).resolves.toBeUndefined();
	});
});
