import type { AnyWidget, Experimental, Host } from "@anywidget/types";

import { scopedModel, type BridgeModel } from "./model";
import { loadWidget } from "./widget-definition";
import { abortable } from "./abort";
import { isCallable, isString, type WidgetValue } from "./runtime-value";
import type { QueuedToolCall } from "./tool-calls";

type LifecycleValue = WidgetValue | void;
type Cleanup = () => LifecycleValue;

export type { Experimental, Host, ResolvedWidget } from "@anywidget/types";
export type ExperimentalInvokeOptions = NonNullable<Parameters<Experimental["invoke"]>[2]>;

export interface InitializeProtocolScope {
	readonly call: QueuedToolCall;
	active: boolean;
	tail: Promise<void>;
	error?: unknown;
}

export type WidgetDefinition = Exclude<AnyWidget, (...args: never[]) => object>;

export interface BindingRuntime {
	host(signal: AbortSignal): Host;
	experimental(
		model: BridgeModel,
		signal: AbortSignal,
		scope?: InitializeProtocolScope,
	): Experimental;
}

export interface RuntimeBinding {
	initialize(call?: QueuedToolCall): Promise<void>;
	render(element: HTMLElement, parentSignal: AbortSignal): Promise<void>;
	getExports(): Promise<object | undefined>;
	dispose(): Promise<void>;
}

interface WidgetBindingOptions {
	reportError(cause: unknown): void;
	loadWidget?: (source: string, signal?: AbortSignal) => Promise<WidgetDefinition>;
	replaceCss?: (css: string | undefined, modelId: string, signal?: AbortSignal) => Promise<void>;
	cleanupTimeoutMilliseconds?: number;
}

interface ActiveView {
	element: HTMLElement;
	parentSignal: AbortSignal;
	owner: object;
	generation?: BindingGeneration;
	remove(): void;
	release(): void;
}

interface BindingGeneration {
	controller: AbortController;
	signal: AbortSignal;
	definition: WidgetDefinition;
	exports: object | undefined;
	initializeCleanup?: Cleanup;
	renderTasks: Set<Promise<void>>;
	cleanupTasks: Set<Promise<void>>;
}

// The latest binding to render an element owns its contents. Older cleanup
// must not clear output installed by a replacement binding.
const viewOwners = new WeakMap<HTMLElement, object>();

function ownsView(view: ActiveView): boolean {
	return viewOwners.get(view.element) === view.owner;
}

function clearOwnedView(view: ActiveView, release: boolean): void {
	if (!ownsView(view)) return;
	view.element.replaceChildren();
	if (release) viewOwners.delete(view.element);
}

export class WidgetBinding implements RuntimeBinding {
	private readonly controller = new AbortController();
	private readonly views = new Set<ActiveView>();
	private readonly reportError: (cause: unknown) => void;
	private readonly loadWidget: (source: string, signal?: AbortSignal) => Promise<WidgetDefinition>;
	private readonly replaceCss: (
		css: string | undefined,
		modelId: string,
		signal?: AbortSignal,
	) => Promise<void>;
	private readonly cleanupTimeoutMilliseconds: number;
	private readonly ready: Promise<void>;
	private resolveReady!: () => void;
	private rejectReady!: (cause: unknown) => void;
	private generation?: BindingGeneration;
	private lifecycleTask: Promise<void> = Promise.resolve();
	private cssTask: Promise<void> = Promise.resolve();
	private esmController?: AbortController;
	private cssController?: AbortController;
	private readonly teardownTasks = new Set<Promise<void>>();
	private esmVersion = 0;
	private cssVersion = 0;
	private listening = false;
	private disposeTask?: Promise<void>;
	private disposed = false;

	constructor(
		private readonly runtime: BindingRuntime,
		private readonly model: BridgeModel,
		options: WidgetBindingOptions,
	) {
		this.reportError = options.reportError;
		this.loadWidget = options.loadWidget ?? loadWidget;
		this.replaceCss = options.replaceCss ?? replaceCss;
		this.cleanupTimeoutMilliseconds = options.cleanupTimeoutMilliseconds ?? 3000;
		this.ready = new Promise<void>((resolve, reject) => {
			this.resolveReady = resolve;
			this.rejectReady = reject;
		});
		void this.ready.catch(() => undefined);
	}

	async initialize(call?: QueuedToolCall): Promise<void> {
		const initialization = this.initializeSources(call);
		try {
			await initialization;
			this.model.finishInitialization();
			this.listen();
			this.resolveReady();
		} catch (error) {
			this.rejectReady(error);
			this.esmController?.abort(error);
			this.cssController?.abort(error);
			await this.dispose();
			throw error;
		}
	}

	private async initializeSources(call?: QueuedToolCall): Promise<void> {
		const css = this.currentCss();
		await this.startCssUpdate(css, false);
		const esm = this.currentEsm();
		await this.startEsmUpdate(esm, false, call);

		// Initializer commands can apply source updates before live listeners are
		// installed. Reconcile those authoritative values before exposing readiness.
		await this.reconcileInitialSources(css, esm, call);
	}

	async render(element: HTMLElement, parentSignal: AbortSignal): Promise<void> {
		await this.ready;
		if (this.disposed || parentSignal.aborted) return;

		let removed = false;
		const owner = {};
		const view: ActiveView = {
			element,
			parentSignal,
			owner,
			remove: () => {
				if (removed) return;
				removed = true;
				parentSignal.removeEventListener("abort", view.release);
				this.views.delete(view);
			},
			release: () => {
				view.remove();
				if (ownsView(view)) viewOwners.delete(element);
			},
		};
		viewOwners.set(element, owner);
		this.views.add(view);
		parentSignal.addEventListener("abort", view.release, { once: true });

		await this.lifecycleTask.catch(() => undefined);
		const generation = this.generation;
		if (generation) await this.renderView(view, generation);
	}

	async getExports(): Promise<object | undefined> {
		await this.ready;
		await this.lifecycleTask.catch(() => undefined);
		return this.generation?.exports;
	}

	dispose(): Promise<void> {
		if (this.disposeTask) return this.disposeTask;
		this.disposed = true;
		const task = this.finishDispose();
		this.disposeTask = task;
		return task;
	}

	private async finishDispose(): Promise<void> {
		this.model.off("change:_css", this.handleCssChange);
		this.model.off("change:_esm", this.handleEsmChange);
		this.controller.abort();
		this.esmController?.abort();
		this.cssController?.abort();
		const views = Array.from(this.views);
		for (const view of views) view.remove();
		this.views.clear();

		const generation = this.generation;
		this.generation = undefined;
		if (generation) this.trackTeardown(this.destroyGeneration(generation));

		await Promise.allSettled([this.lifecycleTask, this.cssTask]);
		await this.joinTeardownTasks();
		for (const view of views) clearOwnedView(view, true);
		const controller = new AbortController();
		try {
			await waitForTask(
				Promise.resolve().then(() =>
					this.replaceCss(undefined, this.model.modelId, controller.signal),
				),
				controller.signal,
				this.cleanupTimeoutMilliseconds,
				"anywidget CSS cleanup",
			);
		} catch (error) {
			console.error("Failed to clean up anywidget styles", error);
		} finally {
			controller.abort();
		}
	}

	private listen(): void {
		if (this.listening || this.disposed) return;
		this.listening = true;
		this.model.on("change:_css", this.handleCssChange);
		this.model.on("change:_esm", this.handleEsmChange);
	}

	private readonly handleCssChange = (): void => {
		if (this.disposed) return;
		void this.startCssUpdate(this.currentCss(), true);
	};

	private readonly handleEsmChange = (): void => {
		if (this.disposed) return;
		let source: string;
		try {
			source = this.currentEsm();
		} catch (error) {
			this.reportError(error);
			return;
		}

		void this.startEsmUpdate(source, true);
	};

	private currentCss(): string | undefined {
		const value = this.model.get("_css");
		return isString(value) && value.length > 0 ? value : undefined;
	}

	private currentEsm(): string {
		const value = this.model.get("_esm");
		if (!isString(value) || value.length === 0) {
			throw new Error(`Missing ESM for model ${this.model.modelId}`);
		}
		return value;
	}

	private async reconcileInitialSources(
		css: string | undefined,
		esm: string,
		call?: QueuedToolCall,
	): Promise<void> {
		// Each generation reads the state committed by the previous initializer.
		/* eslint-disable no-await-in-loop */
		while (!this.disposed) {
			const nextCss = this.currentCss();
			const nextEsm = this.currentEsm();
			if (nextCss === css && nextEsm === esm) return;
			// Yield between generations so source-changing initializers leave runtime
			// disposal and browser input able to cancel initialization.
			await abortable(
				new Promise<void>((resolve) => globalThis.setTimeout(resolve, 0)),
				this.controller.signal,
			);
			if (nextCss !== css) await this.startCssUpdate(nextCss, false);
			if (nextEsm !== esm) await this.startEsmUpdate(nextEsm, false, call);
			css = nextCss;
			esm = nextEsm;
		}
		/* eslint-enable no-await-in-loop */
	}

	private startCssUpdate(css: string | undefined, report: boolean): Promise<void> {
		this.cssController?.abort();
		const controller = new AbortController();
		const version = ++this.cssVersion;
		this.cssController = controller;
		const signal = AbortSignal.any([this.controller.signal, controller.signal]);
		const raw = (async () => {
			try {
				await abortable(
					Promise.resolve().then(() => this.replaceCss(css, this.model.modelId, signal)),
					signal,
				);
			} catch (error) {
				controller.abort(error);
				throw error;
			}
		})();
		this.cssTask = raw.catch((error) => {
			if (report && !this.disposed && version === this.cssVersion) this.reportError(error);
			if (!report && !this.disposed && version === this.cssVersion) throw error;
		});
		return this.cssTask;
	}

	private startEsmUpdate(source: string, report: boolean, call?: QueuedToolCall): Promise<void> {
		this.esmController?.abort();
		const controller = new AbortController();
		const version = ++this.esmVersion;
		this.esmController = controller;
		const raw = this.replaceGeneration(source, controller, version, call);
		this.lifecycleTask = raw.catch((error) => {
			if (report && !this.disposed && version === this.esmVersion) this.reportError(error);
			if (!report && !this.disposed && version === this.esmVersion) throw error;
		});
		return this.lifecycleTask;
	}

	private async replaceGeneration(
		source: string,
		controller: AbortController,
		version: number,
		call?: QueuedToolCall,
	): Promise<void> {
		const previous = this.generation;
		this.generation = undefined;
		if (previous) {
			for (const view of this.views) {
				if (view.generation !== previous) continue;
				view.generation = undefined;
			}
			this.trackTeardown(this.destroyGeneration(previous));
		}
		if (this.disposed || controller.signal.aborted) return;

		let next: BindingGeneration;
		try {
			next = await this.createGeneration(source, controller, call);
		} catch (error) {
			if (!this.disposed && version === this.esmVersion) {
				await this.joinTeardownTasks();
				if (!this.disposed && version === this.esmVersion) {
					for (const view of this.views) clearOwnedView(view, false);
				}
			}
			throw error;
		}
		if (this.disposed || controller.signal.aborted || version !== this.esmVersion) {
			this.trackTeardown(this.destroyGeneration(next));
			return;
		}
		// A replacement may initialize during teardown, but it cannot render until
		// prior generation cleanup settles.
		await this.joinTeardownTasks();
		if (this.disposed || controller.signal.aborted || version !== this.esmVersion) {
			this.trackTeardown(this.destroyGeneration(next));
			return;
		}
		this.generation = next;

		const rendered = await Promise.allSettled(
			Array.from(this.views, (view) => this.renderView(view, next)),
		);
		const failed = rendered.find(
			(result): result is PromiseRejectedResult => result.status === "rejected",
		);
		if (failed) throw failed.reason;
	}

	private async createGeneration(
		source: string,
		controller: AbortController,
		call?: QueuedToolCall,
	): Promise<BindingGeneration> {
		const signal = AbortSignal.any([this.controller.signal, controller.signal]);
		try {
			const definition = await abortable(
				Promise.resolve().then(() => this.loadWidget(source, signal)),
				signal,
			);
			signal.throwIfAborted();
			// Initializer commands reuse the active tool call. Waiting for the global
			// scheduler would block the transaction that is creating this model.
			const protocolScope: InitializeProtocolScope | undefined = call
				? { call, active: true, tail: Promise.resolve() }
				: undefined;
			let initialize: Promise<LifecycleValue> | undefined;
			let result: LifecycleValue;
			let initializeFailed = false;
			let initializeError: unknown;
			try {
				initialize = Promise.resolve(
					definition.initialize?.({
						model: scopedModel(this.model, signal),
						signal,
						experimental: this.runtime.experimental(this.model, signal, protocolScope),
					}),
				);
				result = await abortable(initialize, signal);
			} catch (error) {
				controller.abort(error);
				if (initialize) this.trackLateInitialize(initialize);
				initializeFailed = true;
				initializeError = error;
			}
			if (protocolScope) {
				protocolScope.active = false;
				try {
					await abortable(protocolScope.tail, signal);
				} catch (error) {
					controller.abort(error);
					await protocolScope.tail;
					if (!initializeFailed) {
						initializeFailed = true;
						initializeError = error;
					}
				}
				if (!initializeFailed && protocolScope.error !== undefined) {
					initializeFailed = true;
					initializeError = protocolScope.error;
				}
			}
			if (initializeFailed) {
				if (isCleanup(result)) {
					await runCleanup(result, "anywidget model", this.cleanupTimeoutMilliseconds);
				}
				throw initializeError;
			}
			if (signal.aborted) {
				if (isCleanup(result)) {
					await runCleanup(result, "anywidget model", this.cleanupTimeoutMilliseconds);
				}
				signal.throwIfAborted();
			}
			return {
				controller,
				signal,
				definition,
				// AFM exports retain arbitrary objects, including instances and collections.
				// eslint-disable-next-line anti-slop/no-runtime-typeof -- AFM discriminates initialize results by their JavaScript type.
				exports: typeof result === "object" && result !== null ? result : undefined,
				initializeCleanup: isCleanup(result) ? result : undefined,
				renderTasks: new Set(),
				cleanupTasks: new Set(),
			};
		} catch (error) {
			controller.abort(error);
			throw error;
		}
	}

	private trackLateInitialize(initialize: Promise<LifecycleValue>): void {
		this.trackTeardown(
			initialize.then(
				(lateResult) => {
					if (isCleanup(lateResult)) {
						return runCleanup(lateResult, "late anywidget model", this.cleanupTimeoutMilliseconds);
					}
				},
				() => undefined,
			),
		);
	}

	private async renderView(view: ActiveView, generation: BindingGeneration): Promise<void> {
		if (
			this.disposed ||
			view.parentSignal.aborted ||
			generation.signal.aborted ||
			this.generation !== generation ||
			view.generation === generation
		)
			return;
		if (!ownsView(view)) {
			view.remove();
			return;
		}

		view.generation = generation;
		clearOwnedView(view, false);
		const controller = new AbortController();
		const signal = AbortSignal.any([view.parentSignal, generation.signal, controller.signal]);
		const task = this.runRender(view.element, generation, signal);
		generation.renderTasks.add(task);
		try {
			await task;
		} catch (error) {
			controller.abort(error);
			if (view.generation === generation) view.generation = undefined;
			throw error;
		} finally {
			generation.renderTasks.delete(task);
		}
	}

	private async runRender(
		element: HTMLElement,
		generation: BindingGeneration,
		signal: AbortSignal,
	): Promise<void> {
		const render = Promise.resolve(
			generation.definition.render?.({
				model: scopedModel(this.model, signal),
				el: element,
				signal,
				host: this.runtime.host(signal),
				experimental: this.runtime.experimental(this.model, signal),
			}),
		);
		let cleanup: LifecycleValue;
		try {
			cleanup = await abortable(render, signal);
		} catch (error) {
			this.trackTeardown(
				render.then(
					(lateCleanup) => {
						if (isCleanup(lateCleanup)) {
							return runCleanup(
								lateCleanup,
								"late anywidget view",
								this.cleanupTimeoutMilliseconds,
							);
						}
					},
					() => undefined,
				),
			);
			throw error;
		}
		if (!isCleanup(cleanup)) return;

		let cleaned = false;
		const dispose = async (): Promise<void> => {
			if (cleaned) return;
			cleaned = true;
			signal.removeEventListener("abort", handleAbort);
			const task = runCleanup(cleanup, "anywidget view", this.cleanupTimeoutMilliseconds);
			generation.cleanupTasks.add(task);
			try {
				await task;
			} finally {
				generation.cleanupTasks.delete(task);
			}
		};
		const handleAbort = () => void dispose();
		if (signal.aborted) await dispose();
		else signal.addEventListener("abort", handleAbort, { once: true });
	}

	private async destroyGeneration(generation: BindingGeneration): Promise<void> {
		generation.controller.abort();
		await settleWithin(
			Promise.allSettled(Array.from(generation.renderTasks)),
			this.cleanupTimeoutMilliseconds,
		);
		await settleWithin(
			Promise.allSettled(Array.from(generation.cleanupTasks)),
			this.cleanupTimeoutMilliseconds,
		);
		if (generation.initializeCleanup) {
			await runCleanup(
				generation.initializeCleanup,
				"anywidget model",
				this.cleanupTimeoutMilliseconds,
			);
		}
	}

	// Keep teardown promises reachable because cancelled initialize and render
	// calls can still settle with cleanup functions.
	private trackTeardown(task: Promise<void>): void {
		const tracked = task.catch((error) => {
			console.error("Failed to destroy anywidget generation", error);
		});
		this.teardownTasks.add(tracked);
		void tracked.then(() => this.teardownTasks.delete(tracked));
	}

	private joinTeardownTasks(
		deadline = Date.now() + this.cleanupTimeoutMilliseconds,
	): Promise<void> {
		if (this.teardownTasks.size === 0) return Promise.resolve();
		const remaining = deadline - Date.now();
		if (remaining <= 0) return Promise.resolve();
		return settleWithin(Promise.allSettled(Array.from(this.teardownTasks)), remaining).then(
			(settled) => {
				if (!settled) return;
				return this.joinTeardownTasks(deadline);
			},
		);
	}
}

export async function replaceCss(
	css: string | undefined,
	modelId: string,
	signal?: AbortSignal,
): Promise<void> {
	signal?.throwIfAborted();
	const id = styleId(modelId);
	const previous = document.getElementById(id);
	if (!css) {
		previous?.remove();
		return;
	}

	if (!isUrl(css)) {
		if (previous?.tagName === "STYLE") {
			previous.textContent = css;
			return;
		}
		const style = document.createElement("style");
		style.id = id;
		style.textContent = css;
		if (previous) previous.replaceWith(style);
		else document.head.append(style);
		return;
	}

	const link = document.createElement("link");
	link.rel = "stylesheet";
	link.href = css;
	await new Promise<void>((resolve, reject) => {
		let settled = false;
		const finish = (cause?: unknown): void => {
			if (settled) return;
			settled = true;
			signal?.removeEventListener("abort", abort);
			link.onload = null;
			link.onerror = null;
			if (cause === undefined) resolve();
			else reject(cause);
		};
		const abort = (): void => {
			link.remove();
			finish(signal?.reason ?? new Error("Widget CSS load cancelled"));
		};
		link.onload = () => finish();
		link.onerror = () => {
			link.remove();
			finish(new Error(`Failed to load widget CSS from ${css}`));
		};
		signal?.addEventListener("abort", abort, { once: true });
		if (signal?.aborted) {
			abort();
			return;
		}
		document.head.append(link);
	});
	signal?.throwIfAborted();
	previous?.remove();
	link.id = id;
}

function isUrl(value: string): boolean {
	return /^(https?:|data:|blob:)/.test(value);
}

function styleId(modelId: string): string {
	return `anywidget-style-${modelId.replace(/[^a-zA-Z0-9_-]/g, "-")}`;
}

function isCleanup(value: LifecycleValue): value is Cleanup {
	return isCallable(value);
}

async function runCleanup(
	cleanup: Cleanup,
	label: string,
	timeoutMilliseconds: number,
): Promise<void> {
	try {
		await waitForTask(
			Promise.resolve().then(cleanup),
			undefined,
			timeoutMilliseconds,
			`${label} cleanup`,
		);
	} catch (error) {
		console.error(`Failed to clean up ${label}`, error);
	}
}

function waitForTask<T>(
	task: Promise<T>,
	signal: AbortSignal | undefined,
	timeoutMilliseconds: number | undefined,
	label: string,
): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		let settled = false;
		const finish = (callback: () => void): void => {
			if (settled) return;
			settled = true;
			if (timeout !== undefined) globalThis.clearTimeout(timeout);
			signal?.removeEventListener("abort", abort);
			callback();
		};
		const abort = (): void =>
			finish(() => {
				reject(signal?.reason ?? new Error(`${label} cancelled`));
			});
		const timeout =
			timeoutMilliseconds === undefined
				? undefined
				: globalThis.setTimeout(
						() => finish(() => reject(new Error(`${label} timed out`))),
						timeoutMilliseconds,
					);
		if (signal?.aborted) {
			abort();
			return;
		}
		signal?.addEventListener("abort", abort, { once: true });
		void task.then(
			(value) => finish(() => resolve(value)),
			(error) => finish(() => reject(error)),
		);
	});
}

async function settleWithin<Result>(
	task: Promise<Result>,
	timeoutMilliseconds: number,
): Promise<boolean> {
	return new Promise<boolean>((resolve) => {
		let settled = false;
		const finish = (completed: boolean): void => {
			if (settled) return;
			settled = true;
			globalThis.clearTimeout(timeout);
			resolve(completed);
		};
		const timeout = globalThis.setTimeout(() => finish(false), timeoutMilliseconds);
		void task.then(
			() => finish(true),
			() => finish(true),
		);
	});
}
