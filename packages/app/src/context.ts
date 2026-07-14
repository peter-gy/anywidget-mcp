import type { App } from "@modelcontextprotocol/ext-apps";

import type { State } from "./model";

export interface ModelContextSnapshot {
	version: number;
	tool: string;
	state: State;
}

const MAX_UPDATE_RETRIES = 2;

type ContextApp = Pick<App, "getHostCapabilities" | "updateModelContext"> &
	Partial<Pick<App, "getHostVersion">>;

interface ContextCoordinator {
	nextEpoch: number;
	owner?: ModelContextSync;
}

const contextCoordinators = new WeakMap<object, ContextCoordinator>();

export class ModelContextSync {
	private readonly controller = new AbortController();
	private readonly coordinator: ContextCoordinator;
	private readonly epoch: number;
	private pending?: ModelContextSnapshot;
	private timer?: ReturnType<typeof setTimeout>;
	private inFlight?: Promise<void>;
	private readonly requestTasks = new Set<Promise<unknown>>();
	private latestVersion = 0;
	private retryCount = 0;
	private retryDelay?: number;
	private latestSnapshot?: ModelContextSnapshot;
	private disposeTask?: Promise<void>;
	private disposed = false;
	private readonly readiness: Promise<void>;

	constructor(
		private readonly app: ContextApp,
		ready: Promise<void>,
		private readonly reportError: (error: unknown) => void,
		private readonly debounceMilliseconds = 250,
		private readonly updateTimeoutMilliseconds = 3000,
	) {
		this.coordinator = contextCoordinator(app);
		this.epoch = ++this.coordinator.nextEpoch;
		this.coordinator.owner = this;
		this.readiness = waitUntilReady(ready, this.controller.signal);
		void this.readiness.catch(() => undefined);
	}

	enqueue(snapshot: ModelContextSnapshot): void {
		if (this.disposed || snapshot.version <= this.latestVersion) return;
		this.latestVersion = snapshot.version;
		this.latestSnapshot = snapshot;
		this.retryCount = 0;
		this.retryDelay = undefined;
		this.pending = snapshot;
		if (!this.inFlight) this.schedule();
	}

	dispose(): Promise<void> {
		if (this.disposeTask) return this.disposeTask;
		this.disposed = true;
		if (this.coordinator.owner === this) this.coordinator.owner = undefined;
		this.controller.abort();
		this.pending = undefined;
		if (this.timer) clearTimeout(this.timer);
		this.timer = undefined;
		const tasks = [this.inFlight, ...this.requestTasks].filter(
			(task): task is Promise<unknown> => task !== undefined,
		);
		const task =
			tasks.length > 0
				? settleWithin(Promise.allSettled(tasks), this.updateTimeoutMilliseconds)
				: Promise.resolve();
		this.disposeTask = task;
		return task;
	}

	private schedule(delay = this.debounceMilliseconds): void {
		if (this.timer) clearTimeout(this.timer);
		this.timer = setTimeout(() => {
			this.timer = undefined;
			this.start();
		}, delay);
	}

	private start(): void {
		if (this.disposed || this.inFlight || !this.pending) return;
		const snapshot = this.pending;
		this.pending = undefined;
		const task = this.update(snapshot);
		this.inFlight = task;
		void task.finally(() => {
			if (this.inFlight !== task) return;
			this.inFlight = undefined;
			if (this.pending && !this.disposed) {
				const delay = this.retryDelay ?? this.debounceMilliseconds;
				this.retryDelay = undefined;
				this.schedule(delay);
			}
		});
	}

	private async update(snapshot: ModelContextSnapshot): Promise<void> {
		try {
			await this.readiness;
			if (this.disposed) return;
			const capability = this.app.getHostCapabilities()?.updateModelContext;
			const state = { tool: snapshot.tool, state: snapshot.state };
			if (!capability) {
				// Inspector 12.0.3 implements this request but omits the capability
				// from its initialization response.
				if (this.app.getHostVersion?.()?.name !== "mcp-use-inspector") return;
				await this.sendUpdate(
					{
						content: [{ type: "text", text: modelContextText(snapshot) }],
						structuredContent: state,
					},
					snapshot,
				);
				return;
			}

			if (capability.structuredContent) {
				await this.sendUpdate({ structuredContent: state }, snapshot);
				return;
			}
			if (capability.text) {
				await this.sendUpdate(
					{
						content: [{ type: "text", text: modelContextText(snapshot) }],
					},
					snapshot,
				);
			}
		} catch (error) {
			if (this.disposed) return;
			this.reportError(error);
			if (
				snapshot.version === this.latestVersion &&
				!this.pending &&
				this.retryCount < MAX_UPDATE_RETRIES
			) {
				this.retryCount += 1;
				this.retryDelay = this.debounceMilliseconds * 2 ** this.retryCount;
				this.pending = snapshot;
			}
		}
	}

	private async sendUpdate(
		params: Parameters<App["updateModelContext"]>[0],
		snapshot: ModelContextSnapshot,
	): Promise<void> {
		const request = new AbortController();
		const signal = AbortSignal.any([this.controller.signal, request.signal]);
		const task = Promise.resolve().then(() => this.app.updateModelContext(params, { signal }));
		this.requestTasks.add(task);
		void task.then(
			() => this.requestSettled(task),
			() => this.requestSettled(task),
		);
		try {
			await waitForUpdate(task, signal, request, this.updateTimeoutMilliseconds);
		} catch (error) {
			if (
				!this.controller.signal.aborted &&
				request.signal.aborted &&
				request.signal.reason === error
			) {
				void task.then(
					() => this.resendAfterLateSettlement(snapshot.version),
					() => this.resendAfterLateSettlement(snapshot.version),
				);
			}
			throw error;
		} finally {
			request.abort();
		}
	}

	private resendAfterLateSettlement(version: number): void {
		const latest = this.latestSnapshot;
		if (this.disposed || !latest || latest.version <= version) return;
		this.pending = latest;
		this.retryDelay = undefined;
		if (!this.inFlight) this.schedule();
	}

	private requestSettled(task: Promise<unknown>): void {
		this.requestTasks.delete(task);
		const owner = this.coordinator.owner;
		if (!owner || owner.epoch <= this.epoch) return;
		owner.republishLatest();
	}

	private republishLatest(): void {
		if (this.disposed || !this.latestSnapshot) return;
		this.pending = this.latestSnapshot;
		this.retryDelay = undefined;
		if (!this.inFlight) this.schedule();
	}
}

function contextCoordinator(app: ContextApp): ContextCoordinator {
	const existing = contextCoordinators.get(app);
	if (existing) return existing;
	const coordinator = { nextEpoch: 0 };
	contextCoordinators.set(app, coordinator);
	return coordinator;
}

function settleWithin(task: Promise<unknown>, milliseconds: number): Promise<void> {
	return new Promise<void>((resolve) => {
		let settled = false;
		const finish = (): void => {
			if (settled) return;
			settled = true;
			clearTimeout(timeout);
			resolve();
		};
		const timeout = setTimeout(finish, milliseconds);
		void task.then(finish, finish);
	});
}

export function modelContextText(snapshot: ModelContextSnapshot): string {
	return `Current ${snapshot.tool} state: ${JSON.stringify(snapshot.state)}`;
}

function waitUntilReady(ready: Promise<void>, signal: AbortSignal): Promise<void> {
	return new Promise((resolve, reject) => {
		const finish = (): void => {
			signal.removeEventListener("abort", abort);
			resolve();
		};
		const fail = (error: unknown): void => {
			signal.removeEventListener("abort", abort);
			reject(error);
		};
		const abort = (): void => finish();
		if (signal.aborted) {
			resolve();
			return;
		}
		signal.addEventListener("abort", abort, { once: true });
		void ready.then(finish, fail);
	});
}

function waitForUpdate<T>(
	task: Promise<T>,
	signal: AbortSignal,
	request: AbortController,
	timeoutMilliseconds: number,
): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		let settled = false;
		let timeout: ReturnType<typeof setTimeout> | undefined;
		const finish = (callback: () => void): void => {
			if (settled) return;
			settled = true;
			if (timeout) clearTimeout(timeout);
			signal.removeEventListener("abort", abort);
			callback();
		};
		const abort = (): void =>
			finish(() => {
				reject(signal.reason ?? new Error("Model context update cancelled"));
			});
		if (signal.aborted) {
			abort();
			return;
		}
		signal.addEventListener("abort", abort, { once: true });
		timeout = setTimeout(() => {
			request.abort(new Error("Model context update timed out"));
		}, timeoutMilliseconds);
		void task.then(
			(value) => finish(() => resolve(value)),
			(error) => finish(() => reject(error)),
		);
	});
}
