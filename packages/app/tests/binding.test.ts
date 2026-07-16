import { afterEach, describe, expect, test, vi } from "vite-plus/test";

import {
	WidgetBinding,
	type BindingRuntime,
	type Experimental,
	type Host,
	type InitializeProtocolScope,
	type WidgetDefinition,
} from "../src/binding";
import { BridgeModel, type ModelPayload, type ModelRuntime } from "../src/model";
import type { QueuedToolCall } from "../src/tool-calls";

function createModel(state: Record<string, unknown>): BridgeModel {
	let model!: BridgeModel;
	const runtime: ModelRuntime = {
		model: () => model,
		enqueueUpdate: () => undefined,
		enqueueCustom: () => undefined,
	};
	const payload: ModelPayload = {
		modelId: "model-1",
		state,
	};
	model = new BridgeModel(runtime, payload, vi.fn());
	return model;
}

function createRuntime(): BindingRuntime {
	const host = {} as Host;
	const experimental = {} as Experimental;
	return {
		host: () => host,
		experimental: () => experimental,
	};
}

function element(): HTMLElement {
	return { replaceChildren: vi.fn() } as unknown as HTMLElement;
}

function trackedElement(): HTMLElement {
	const children = new Set<Node>();
	return {
		append: (...nodes: Node[]) => {
			for (const node of nodes) children.add(node);
		},
		contains: (node: Node | null) => node !== null && children.has(node),
		replaceChildren: vi.fn((...nodes: Node[]) => {
			children.clear();
			for (const node of nodes) children.add(node);
		}),
	} as unknown as HTMLElement;
}

function deferred<T>(): {
	promise: Promise<T>;
	resolve: (value: T) => void;
} {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((done) => {
		resolve = done;
	});
	return { promise, resolve };
}

describe("WidgetBinding live source lifecycle", () => {
	afterEach(() => {
		vi.useRealTimers();
		vi.restoreAllMocks();
	});

	test("replaces CSS when the synchronized _css trait changes", async () => {
		const model = createModel({ _esm: "first", _css: ".first {}" });
		const replaceCss = vi.fn().mockResolvedValue(undefined);
		const binding = new WidgetBinding(createRuntime(), model, {
			reportError: vi.fn(),
			replaceCss,
			loadWidget: async () => ({}),
		});

		await binding.initialize();
		model.receive({ method: "update", state: { _css: ".second {}" } }, []);

		await vi.waitFor(() => expect(replaceCss).toHaveBeenCalledTimes(2));
		expect(replaceCss.mock.calls.map(([css, modelId]) => [css, modelId])).toEqual([
			[".first {}", "model-1"],
			[".second {}", "model-1"],
		]);

		await binding.dispose();
		expect(replaceCss).toHaveBeenLastCalledWith(undefined, "model-1", expect.any(AbortSignal));
	});

	test("reconciles source updates returned by an initializer command before readiness", async () => {
		const model = createModel({ _esm: "first", _css: ".first {}" });
		const rendered: string[] = [];
		let invoked = false;
		const runtime: BindingRuntime = {
			host: () => ({}) as Host,
			experimental: () => ({
				async invoke<T>() {
					invoked = true;
					model.receive({ method: "update", state: { _esm: "second", _css: ".second {}" } }, []);
					return [{} as T, []];
				},
			}),
		};
		const replaceCss = vi.fn().mockResolvedValue(undefined);
		const loadWidget = vi.fn(
			async (source: string): Promise<WidgetDefinition> => ({
				initialize:
					source === "first"
						? async ({ experimental }) => {
								await experimental.invoke("refresh_source");
							}
						: undefined,
				render: () => {
					rendered.push(source);
				},
			}),
		);
		const binding = new WidgetBinding(runtime, model, {
			reportError: vi.fn(),
			replaceCss,
			loadWidget,
		});

		await binding.initialize();

		expect(invoked).toBe(true);
		expect(loadWidget.mock.calls.map(([source]) => source)).toEqual(["first", "second"]);
		expect(replaceCss.mock.calls.map(([css]) => css)).toEqual([".first {}", ".second {}"]);

		await binding.render(element(), new AbortController().signal);
		expect(rendered).toEqual(["second"]);
		await binding.dispose();
	});

	test("bounds initializer source churn and cleans each generation", async () => {
		const model = createModel({ _esm: "first", _css: ".first {}" });
		const scopes: InitializeProtocolScope[] = [];
		const cleanups: string[] = [];
		const runtime: BindingRuntime = {
			host: () => ({}) as Host,
			experimental: (_model, _signal, scope) => {
				if (scope) scopes.push(scope);
				return {
					async invoke<T>() {
						const first = model.get("_esm") === "first";
						model.receive(
							{
								method: "update",
								state: {
									_esm: first ? "second" : "first",
									_css: first ? ".second {}" : ".first {}",
								},
							},
							[],
						);
						return [{} as T, []];
					},
				};
			},
		};
		const replaceCss = vi.fn().mockResolvedValue(undefined);
		const loadWidget = vi.fn(
			async (source: string): Promise<WidgetDefinition> => ({
				async initialize({ experimental }) {
					await experimental.invoke("toggle_source");
					return () => cleanups.push(source);
				},
			}),
		);
		const binding = new WidgetBinding(runtime, model, {
			reportError: vi.fn(),
			replaceCss,
			loadWidget,
			timeoutMilliseconds: 10,
		});
		const call: QueuedToolCall = async () => ({ content: [] });

		await expect(binding.initialize(call)).rejects.toThrow(
			"Widget sources did not converge during initialization",
		);

		expect(loadWidget.mock.calls.map(([source]) => source)).toEqual(["first", "second"]);
		expect(replaceCss.mock.calls.map(([css]) => css)).toEqual([
			".first {}",
			".second {}",
			undefined,
		]);
		expect(cleanups).toEqual(["first", "second"]);
		expect(scopes).toHaveLength(2);
		expect(scopes.every((scope) => !scope.active)).toBe(true);
		await expect(binding.dispose()).resolves.toBeUndefined();
	}, 1000);

	test("cleans up the prior generation before rerendering active views", async () => {
		const events: string[] = [];
		const first: WidgetDefinition = {
			initialize: () => {
				events.push("initialize:first");
				return () => events.push("cleanup-model:first");
			},
			render: () => {
				events.push("render:first");
				return () => events.push("cleanup-view:first");
			},
		};
		const second: WidgetDefinition = {
			initialize: () => {
				events.push("initialize:second");
				return () => events.push("cleanup-model:second");
			},
			render: () => {
				events.push("render:second");
				return () => events.push("cleanup-view:second");
			},
		};
		const loadWidget = vi.fn(async (source: string) => (source === "first" ? first : second));
		const model = createModel({ _esm: "first" });
		const binding = new WidgetBinding(createRuntime(), model, {
			reportError: vi.fn(),
			replaceCss: async () => undefined,
			loadWidget,
		});
		const root = element();

		await binding.initialize();
		await binding.render(root, new AbortController().signal);
		model.receive({ method: "update", state: { _esm: "second" } }, []);

		await vi.waitFor(() => expect(events).toContain("render:second"));
		expect(events).toEqual([
			"initialize:first",
			"render:first",
			"cleanup-view:first",
			"initialize:second",
			"cleanup-model:first",
			"render:second",
		]);
		expect(loadWidget).toHaveBeenCalledWith("second", expect.any(AbortSignal));

		await binding.dispose();
		expect(events.slice(-2)).toEqual(["cleanup-view:second", "cleanup-model:second"]);
	});

	test("keeps a rendered mount present through hot-reload cleanup", async () => {
		const model = createModel({ _esm: "first" });
		const root = trackedElement();
		const mount = {} as Node;
		const cleanup = deferred<void>();
		let cleanupStarted = false;
		let presentAfterCleanup: boolean | undefined;
		let replacementRendered = false;
		const binding = new WidgetBinding(createRuntime(), model, {
			reportError: vi.fn(),
			replaceCss: async () => undefined,
			loadWidget: async (source) => ({
				render: ({ el }) => {
					if (source === "first") {
						el.append(mount);
						return async () => {
							cleanupStarted = true;
							await cleanup.promise;
							presentAfterCleanup = el.contains(mount);
						};
					}
					replacementRendered = true;
				},
			}),
		});

		await binding.initialize();
		await binding.render(root, new AbortController().signal);
		model.receive({ method: "update", state: { _esm: "second" } }, []);

		await vi.waitFor(() => expect(cleanupStarted).toBe(true));
		expect(root.contains(mount)).toBe(true);
		expect(replacementRendered).toBe(false);
		cleanup.resolve(undefined);
		await vi.waitFor(() => expect(replacementRendered).toBe(true));
		expect(presentAfterCleanup).toBe(true);
		await binding.dispose();
	});

	test("keeps a rendered mount present through disposal cleanup", async () => {
		const root = trackedElement();
		const mount = {} as Node;
		const cleanup = deferred<void>();
		let cleanupStarted = false;
		let presentAfterCleanup: boolean | undefined;
		const binding = new WidgetBinding(createRuntime(), createModel({ _esm: "first" }), {
			reportError: vi.fn(),
			replaceCss: async () => undefined,
			loadWidget: async () => ({
				render: ({ el }) => {
					el.append(mount);
					return async () => {
						cleanupStarted = true;
						await cleanup.promise;
						presentAfterCleanup = el.contains(mount);
					};
				},
			}),
		});

		await binding.initialize();
		await binding.render(root, new AbortController().signal);
		const disposal = binding.dispose();

		await vi.waitFor(() => expect(cleanupStarted).toBe(true));
		expect(root.contains(mount)).toBe(true);
		cleanup.resolve(undefined);
		await disposal;
		expect(presentAfterCleanup).toBe(true);
		expect(root.contains(mount)).toBe(false);
	});

	test("preserves replacement-owned content when an old binding finishes disposal", async () => {
		const root = trackedElement();
		const oldMount = {} as Node;
		const replacementMount = {} as Node;
		const cleanup = deferred<void>();
		let cleanupStarted = false;
		const oldBinding = new WidgetBinding(createRuntime(), createModel({ _esm: "old" }), {
			reportError: vi.fn(),
			replaceCss: async () => undefined,
			loadWidget: async () => ({
				render: ({ el }) => {
					el.append(oldMount);
					return async () => {
						cleanupStarted = true;
						await cleanup.promise;
					};
				},
			}),
		});
		const replacement = new WidgetBinding(createRuntime(), createModel({ _esm: "replacement" }), {
			reportError: vi.fn(),
			replaceCss: async () => undefined,
			loadWidget: async () => ({
				render: ({ el }) => el.append(replacementMount),
			}),
		});

		await oldBinding.initialize();
		await oldBinding.render(root, new AbortController().signal);
		const oldDisposal = oldBinding.dispose();
		await vi.waitFor(() => expect(cleanupStarted).toBe(true));
		expect(root.contains(oldMount)).toBe(true);

		await replacement.initialize();
		await replacement.render(root, new AbortController().signal);
		expect(root.contains(replacementMount)).toBe(true);

		cleanup.resolve(undefined);
		await oldDisposal;
		expect(root.contains(replacementMount)).toBe(true);

		await replacement.dispose();
	});

	test("clears an owned stale mount after a failed hot reload finishes cleanup", async () => {
		const model = createModel({ _esm: "working" });
		const root = trackedElement();
		const mount = {} as Node;
		const cleanup = deferred<void>();
		const reportError = vi.fn();
		let cleanupStarted = false;
		let presentAfterCleanup: boolean | undefined;
		const binding = new WidgetBinding(createRuntime(), model, {
			reportError,
			replaceCss: async () => undefined,
			loadWidget: async (source) => {
				if (source === "broken") throw new Error("broken source");
				return {
					render: ({ el }: { el: HTMLElement }) => {
						el.append(mount);
						return async () => {
							cleanupStarted = true;
							await cleanup.promise;
							presentAfterCleanup = el.contains(mount);
						};
					},
				};
			},
		});

		await binding.initialize();
		await binding.render(root, new AbortController().signal);
		model.receive({ method: "update", state: { _esm: "broken" } }, []);

		await vi.waitFor(() => expect(cleanupStarted).toBe(true));
		expect(root.contains(mount)).toBe(true);
		cleanup.resolve(undefined);
		await vi.waitFor(() => expect(reportError).toHaveBeenCalledWith(expect.any(Error)));

		expect(presentAfterCleanup).toBe(true);
		expect(root.contains(mount)).toBe(false);
		await binding.dispose();
	});

	test("scopes initialize and render experimental APIs to their generation", async () => {
		const captured: Experimental[] = [];
		const runtime: BindingRuntime = {
			host: () => ({}) as Host,
			experimental: (_model, signal) => ({
				async invoke<T>() {
					signal.throwIfAborted();
					return [{} as T, []];
				},
			}),
		};
		const definition: WidgetDefinition = {
			initialize({ experimental }) {
				captured.push(experimental);
			},
			render({ experimental }) {
				captured.push(experimental);
			},
		};
		const loadWidget = vi.fn().mockResolvedValue(definition);
		const model = createModel({ _esm: "first" });
		const binding = new WidgetBinding(runtime, model, {
			reportError: vi.fn(),
			replaceCss: async () => undefined,
			loadWidget,
		});

		await binding.initialize();
		await binding.render(element(), new AbortController().signal);
		expect(captured).toHaveLength(2);
		model.receive({ method: "update", state: { _esm: "second" } }, []);
		await vi.waitFor(() => expect(loadWidget).toHaveBeenCalledTimes(2));

		await expect(captured[0]?.invoke("stale")).rejects.toMatchObject({ name: "AbortError" });
		await expect(captured[1]?.invoke("stale")).rejects.toMatchObject({ name: "AbortError" });
		await binding.dispose();
	});

	test("starts the latest ESM and CSS while stale loads remain pending", async () => {
		const stalledEsm = deferred<WidgetDefinition>();
		const stalledCss = deferred<void>();
		const rendered: string[] = [];
		const definition = (name: string): WidgetDefinition => ({
			render: () => {
				rendered.push(name);
			},
		});
		const loadWidget = vi.fn((source: string) => {
			if (source === "stalled") return stalledEsm.promise;
			return Promise.resolve(definition(source));
		});
		const replaceCss = vi.fn((css: string | undefined) => {
			if (css === ".stalled {}") return stalledCss.promise;
			return Promise.resolve();
		});
		const reportError = vi.fn();
		const model = createModel({ _esm: "first", _css: ".first {}" });
		const binding = new WidgetBinding(createRuntime(), model, {
			reportError,
			loadWidget,
			replaceCss,
			timeoutMilliseconds: 1000,
		});

		await binding.initialize();
		await binding.render(element(), new AbortController().signal);
		model.receive({ method: "update", state: { _esm: "stalled" } }, []);
		await vi.waitFor(() =>
			expect(loadWidget).toHaveBeenCalledWith("stalled", expect.any(AbortSignal)),
		);
		model.receive({ method: "update", state: { _esm: "latest" } }, []);

		model.receive({ method: "update", state: { _css: ".stalled {}" } }, []);
		await vi.waitFor(() =>
			expect(replaceCss.mock.calls.some(([css]) => css === ".stalled {}")).toBe(true),
		);
		model.receive({ method: "update", state: { _css: ".latest {}" } }, []);

		await vi.waitFor(() => expect(rendered).toEqual(["first", "latest"]));
		await vi.waitFor(() =>
			expect(replaceCss.mock.calls.some(([css]) => css === ".latest {}")).toBe(true),
		);
		expect(reportError).not.toHaveBeenCalled();

		await binding.dispose();
	});

	test("bounds stalled initialize, render, and cleanup work during disposal", async () => {
		vi.useFakeTimers();
		vi.spyOn(console, "error").mockImplementation(() => undefined);
		const never = new Promise<never>(() => undefined);
		const model = createModel({ _esm: "first" });
		const initialize = vi.fn(() => never);
		const initializing = new WidgetBinding(createRuntime(), model, {
			reportError: vi.fn(),
			replaceCss: async () => undefined,
			loadWidget: async () => ({ initialize }),
			timeoutMilliseconds: 10,
		});

		const initialization = initializing.initialize();
		await vi.advanceTimersByTimeAsync(0);
		expect(initialize).toHaveBeenCalledTimes(1);
		const initializationDisposal = initializing.dispose();
		await vi.runAllTimersAsync();
		await expect(initializationDisposal).resolves.toBeUndefined();
		await expect(initialization).resolves.toBeUndefined();

		let renderCount = 0;
		const active = new WidgetBinding(createRuntime(), createModel({ _esm: "active" }), {
			reportError: vi.fn(),
			replaceCss: async () => undefined,
			loadWidget: async () => ({
				initialize: () => () => never,
				render: () => {
					renderCount += 1;
					if (renderCount === 1) return () => never;
					return never;
				},
			}),
			timeoutMilliseconds: 10,
		});
		await active.initialize();
		await active.render(element(), new AbortController().signal);
		const stalledRender = active.render(element(), new AbortController().signal);
		const stalledRenderError = stalledRender.catch((error: unknown) => error);
		await vi.advanceTimersByTimeAsync(0);
		expect(renderCount).toBe(2);
		const disposal = active.dispose();
		await vi.runAllTimersAsync();

		await expect(disposal).resolves.toBeUndefined();
		expect(await stalledRenderError).toBeDefined();
	});

	test("joins cleanup returned after an initializer is cancelled", async () => {
		const lateInitialize = deferred<unknown>();
		const cleanup = vi.fn();
		const initialize = vi.fn(() => lateInitialize.promise);
		const binding = new WidgetBinding(createRuntime(), createModel({ _esm: "first" }), {
			reportError: vi.fn(),
			replaceCss: async () => undefined,
			loadWidget: async () => ({ initialize }),
			timeoutMilliseconds: 1000,
		});

		const initialization = binding.initialize();
		await vi.waitFor(() => expect(initialize).toHaveBeenCalledTimes(1));
		const disposal = binding.dispose();
		expect(binding.dispose()).toBe(disposal);
		let disposed = false;
		void disposal.then(() => {
			disposed = true;
		});
		await Promise.resolve();
		expect(disposed).toBe(false);

		lateInitialize.resolve(cleanup);
		await disposal;
		expect(cleanup).toHaveBeenCalledTimes(1);
		await initialization;
	});

	test("joins cleanup returned after a render is cancelled", async () => {
		const lateRender = deferred<unknown>();
		const cleanup = vi.fn();
		const render = vi.fn(() => lateRender.promise);
		const binding = new WidgetBinding(createRuntime(), createModel({ _esm: "first" }), {
			reportError: vi.fn(),
			replaceCss: async () => undefined,
			loadWidget: async () => ({ render }),
			timeoutMilliseconds: 1000,
		});
		await binding.initialize();
		const rendering = binding.render(element(), new AbortController().signal);
		const renderError = rendering.catch((error: unknown) => error);
		await vi.waitFor(() => expect(render).toHaveBeenCalledTimes(1));

		const disposal = binding.dispose();
		let disposed = false;
		void disposal.then(() => {
			disposed = true;
		});
		await Promise.resolve();
		expect(disposed).toBe(false);

		lateRender.resolve(cleanup);
		await disposal;
		expect(cleanup).toHaveBeenCalledTimes(1);
		expect(await renderError).toBeDefined();
	});
});
