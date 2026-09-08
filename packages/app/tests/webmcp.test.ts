// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, test, vi } from "vite-plus/test";

import type { WidgetDefinition } from "../src/binding";
import type { AnyModel, EventHandler } from "../src/model";
import { isCallable, type RuntimeRecord, type WidgetValue } from "../src/runtime-value";
import { instrument, type WebMCPDescriptor } from "../src/webmcp";
import { deferred } from "./runtime-test-support";

const load = vi.fn<() => Promise<WidgetDefinition>>();

interface Tool {
	name: string;
	inputSchema: object;
	execute(
		input: RuntimeRecord,
		options: { signal?: AbortSignal },
	): Promise<{ state: RuntimeRecord }>;
}

const descriptor: WebMCPDescriptor = {
	id: "counter1",
	title: "Counter",
	description: "A counter with a Python-derived label.",
	properties: { count: { type: "integer" }, label: { type: "string" } },
	writable: { count: { type: "integer" } },
};

function modelFixture(metadata: WidgetValue = descriptor) {
	const listeners = new Set<EventHandler>();
	const metadataListeners = new Set<EventHandler>();
	const connectionListeners = new Set<EventHandler>();
	const model: AnyModel = {
		get: vi.fn((name: string) => (name === "_webmcp" ? metadata : undefined)),
		set: vi.fn(),
		save_changes: vi.fn(),
		send: vi.fn(),
		on: (name, callback) => {
			(name === "msg:custom"
				? listeners
				: name === "comm:close"
					? connectionListeners
					: metadataListeners
			).add(callback);
		},
		off: (name, callback) => {
			if (callback)
				(name === "msg:custom"
					? listeners
					: name === "comm:close"
						? connectionListeners
						: metadataListeners
				).delete(callback);
		},
		widget_manager: {
			async get_model<T extends object>() {
				// SAFETY: The fixture exposes one model for each caller's declared trait schema.
				return model as AnyModel<T>;
			},
		},
	};
	return {
		model,
		listeners,
		metadataListeners,
		closeComm() {
			for (const listener of Array.from(connectionListeners)) listener();
		},
		setMetadata(value: WidgetValue) {
			metadata = value;
			for (const listener of metadataListeners) listener();
		},
		reply(result: RuntimeRecord, index = 0) {
			const message = vi.mocked(model.send).mock.calls[index]?.[0];
			const id: unknown = Object.getOwnPropertyDescriptor(message, "id")?.value;
			for (const listener of listeners) {
				listener({ kind: "anywidget-webmcp-result", id, ...result });
			}
		},
	};
}

function renderOptions(model: AnyModel, signal = new AbortController().signal) {
	return {
		model,
		signal,
		el: document.createElement("div"),
		host: {
			async getModel<T extends object>() {
				// SAFETY: The fixture exposes one model for each caller's declared trait schema.
				return model as AnyModel<T>;
			},
			getWidget: () => Promise.reject(new Error("No child widget")),
		},
		experimental: {
			async invoke() {
				throw new Error("This WebMCP fixture uses model.send for requests");
			},
		},
	};
}

async function dispose(cleanup: WidgetValue | void) {
	if (isCallable(cleanup)) await Promise.resolve(cleanup());
}

describe("WebMCP widget instrumentation", () => {
	const tools = new Map<string, Tool>();
	const controllers: AbortSignal[] = [];
	const registerTool = vi.fn((tool: Tool, { signal }: { signal: AbortSignal }) => {
		signal.throwIfAborted();
		if (tools.has(tool.name)) throw new Error("Duplicate tool");
		tools.set(tool.name, tool);
		controllers.push(signal);
		signal.addEventListener("abort", () => tools.delete(tool.name), { once: true });
		return Promise.resolve();
	});

	beforeEach(() => {
		Object.defineProperty(document, "modelContext", {
			configurable: true,
			value: { registerTool },
		});
		load.mockResolvedValue({ render: vi.fn() });
	});

	afterEach(() => {
		tools.clear();
		controllers.length = 0;
		Reflect.deleteProperty(document, "modelContext");
		vi.resetAllMocks();
		vi.useRealTimers();
	});

	async function mount(overrides: Partial<WebMCPDescriptor> = {}) {
		const fixture = modelFixture({ ...descriptor, ...overrides });
		const widget = await instrument("original source", load);
		const options = renderOptions(fixture.model);
		await Promise.resolve(widget.initialize?.(options));
		const cleanup = await Promise.resolve(widget.render?.(options));
		await Promise.resolve();
		return { ...fixture, widget, options, cleanup };
	}

	function tool(operation: "read" | "update", id = descriptor.id): Tool {
		const result = tools.get(`anywidget_${id}_${operation}`);
		if (!result) throw new Error(`Missing ${operation} tool`);
		return result;
	}

	test("publishes writable schemas and waits for validated state and observer results", async () => {
		const fixture = await mount();
		expect(tool("update").inputSchema).toEqual({
			type: "object",
			properties: { count: { type: "integer" } },
			additionalProperties: false,
			minProperties: 1,
		});
		const finished = vi.fn();
		const result = tool("update").execute({ count: 4 }, {}).then(finished);
		await Promise.resolve();
		expect(fixture.model.send).toHaveBeenCalledWith({
			kind: "anywidget-webmcp",
			id: expect.any(String),
			operation: "update",
			state: { count: 4 },
		});
		expect(finished).not.toHaveBeenCalled();
		fixture.reply({ result: { state: { count: 4, label: "Count: 4" } } });
		await result;
		expect(finished).toHaveBeenCalledWith({ state: { count: 4, label: "Count: 4" } });
		expect(fixture.listeners.size).toBe(0);
		await dispose(fixture.cleanup);
	});

	test("serializes reads behind updates and continues after Python rejects an update", async () => {
		const fixture = await mount();
		const update = tool("update").execute({ count: -1 }, {});
		const rejected = expect(update).rejects.toThrow("Count must be positive");
		const read = tool("read").execute({}, {});
		await Promise.resolve();
		expect(fixture.model.send).toHaveBeenCalledTimes(1);
		fixture.reply({ error: "Count must be positive" });
		await rejected;
		await vi.waitFor(() => expect(fixture.model.send).toHaveBeenCalledTimes(2));
		fixture.reply({ result: { state: { count: 0, label: "Count: 0" } } }, 1);
		await expect(read).resolves.toEqual({ state: { count: 0, label: "Count: 0" } });
		await dispose(fixture.cleanup);
	});

	test("shares registration across views and unregisters when the last view closes", async () => {
		const fixture = await mount();
		const second = await Promise.resolve(
			fixture.widget.render?.(renderOptions(modelFixture().model)),
		);
		expect(registerTool).toHaveBeenCalledTimes(2);
		await dispose(fixture.cleanup);
		expect(tools.size).toBe(2);
		const read = tool("read").execute({}, {});
		await Promise.resolve();
		fixture.reply({ result: { state: { count: 3 } } });
		await expect(read).resolves.toEqual({ state: { count: 3 } });
		await dispose(second);
		expect(tools.size).toBe(0);
	});

	test("keeps instances with identical modules addressable through their model metadata", async () => {
		const first = await mount();
		const second = await mount({ id: "counter2" });
		const read = tool("read", "counter2").execute({}, {});
		await Promise.resolve();
		expect(first.model.send).not.toHaveBeenCalled();
		second.reply({ result: { state: { count: 7 } } });
		await expect(read).resolves.toEqual({ state: { count: 7 } });
		await dispose(first.cleanup);
		expect(tools.size).toBe(2);
		await dispose(second.cleanup);
	});

	test("publishes a state reader for a widget with no writable traits", async () => {
		const fixture = await mount({ writable: {} });
		expect([...tools.keys()]).toEqual(["anywidget_counter1_read"]);
		await dispose(fixture.cleanup);
	});

	test("registers a same-origin embedded widget in the containing page", async () => {
		const frame = document.createElement("iframe");
		document.body.append(frame);
		try {
			const childDocument = frame.contentDocument;
			if (!childDocument) throw new Error("Frame document is unavailable");
			const fixture = modelFixture();
			const widget = await instrument("source", load);
			const options = renderOptions(fixture.model);
			options.el = childDocument.createElement("div");
			await Promise.resolve(widget.initialize?.(options));
			const cleanup = await Promise.resolve(widget.render?.(options));
			expect(tools.size).toBe(2);
			await dispose(cleanup);
		} finally {
			frame.remove();
		}
	});

	test("preserves initialize exports, render arguments, and cleanup", async () => {
		const exports = { selected: () => [1, 3] };
		const cleanup = vi.fn();
		const definition: WidgetDefinition = {
			initialize: vi.fn(() => exports),
			render: vi.fn(() => cleanup),
		};
		load.mockResolvedValue(definition);
		const fixture = modelFixture();
		const widget = await instrument("source URL", load);
		const options = renderOptions(fixture.model);
		expect(await Promise.resolve(widget.initialize?.(options))).toBe(exports);
		const disposeView = await Promise.resolve(widget.render?.(options));
		expect(definition.initialize).toHaveBeenCalledWith(options);
		expect(definition.render).toHaveBeenCalledWith(options);
		await dispose(disposeView);
		await dispose(disposeView);
		expect(cleanup).toHaveBeenCalledOnce();
	});

	test("renders widgets when WebMCP is unavailable", async () => {
		Reflect.deleteProperty(document, "modelContext");
		const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
		const render = vi.fn();
		load.mockResolvedValue({ render });
		const fixture = await mount();
		expect(render).toHaveBeenCalledWith(fixture.options);
		expect(warning).toHaveBeenCalledOnce();
		await dispose(fixture.cleanup);
	});

	test("withdraws an accepted tool when the browser rejects its companion", async () => {
		vi.spyOn(console, "warn").mockImplementation(() => {});
		const permission = deferred<void>();
		const register = registerTool.getMockImplementation();
		if (!register) throw new Error("Missing browser registry");
		registerTool.mockImplementationOnce(register).mockImplementationOnce(async () => {
			await permission.promise;
			throw new Error("Permission denied");
		});
		const fixture = await mount();
		await vi.waitFor(() => expect([...tools.keys()]).toEqual(["anywidget_counter1_read"]));
		permission.resolve();
		await vi.waitFor(() => expect(tools.size).toBe(0));
		await dispose(fixture.cleanup);
	});

	test("aborts pending requests and removes listeners when the last view closes", async () => {
		const fixture = await mount();
		const read = tool("read").execute({}, {});
		const rejected = expect(read).rejects.toMatchObject({ name: "AbortError" });
		await Promise.resolve();
		await dispose(fixture.cleanup);
		await rejected;
		expect(fixture.listeners.size).toBe(0);
		expect(tools.size).toBe(0);
	});

	test("rejects queued cancellation immediately and skips its Python operation", async () => {
		const fixture = await mount();
		const active = tool("read").execute({}, {});
		const controller = new AbortController();
		const queued = tool("update").execute({ count: 9 }, { signal: controller.signal });
		const rejected = expect(queued).rejects.toMatchObject({ name: "AbortError" });
		controller.abort();
		await rejected;
		expect(fixture.model.send).toHaveBeenCalledOnce();
		fixture.reply({ result: { state: { count: 0 } } });
		await active;
		const finalRead = tool("read").execute({}, {});
		await vi.waitFor(() => expect(fixture.model.send).toHaveBeenCalledTimes(2));
		fixture.reply({ result: { state: { count: 0 } } }, 1);
		await finalRead;
		await dispose(fixture.cleanup);
	});

	test("unregisters visible tools when the owning model is disposed", async () => {
		const fixture = modelFixture();
		const widget = await instrument("source", load);
		const controller = new AbortController();
		await Promise.resolve(widget.initialize?.(renderOptions(fixture.model, controller.signal)));
		const cleanup = await Promise.resolve(widget.render?.(renderOptions(fixture.model)));
		expect(tools.size).toBe(2);
		controller.abort();
		expect(tools.size).toBe(0);
		expect(fixture.metadataListeners.size).toBe(0);
		await dispose(cleanup);
	});

	test("withdraws and restores tools for existing views when Python changes exposure", async () => {
		const fixture = await mount();
		const secondView = await Promise.resolve(
			fixture.widget.render?.(renderOptions(modelFixture().model)),
		);
		const pending = tool("read").execute({}, {});
		const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
		await Promise.resolve();
		fixture.setMetadata(null);
		await rejected;
		expect(tools.size).toBe(0);
		expect(fixture.listeners.size).toBe(0);
		fixture.setMetadata(descriptor);
		await vi.waitFor(() => expect(tools.size).toBe(2));
		await dispose(fixture.cleanup);
		expect(tools.size).toBe(2);
		const read = tool("read").execute({}, {});
		await Promise.resolve();
		fixture.reply({ result: { state: { count: 4 } } }, 1);
		await expect(read).resolves.toEqual({ state: { count: 4 } });
		await dispose(secondView);
		expect(tools.size).toBe(0);
	});

	test("refreshes tool schemas for dynamically added synchronized traits", async () => {
		const fixture = await mount();
		const previous = tool("update");
		fixture.setMetadata({
			...descriptor,
			properties: { ...descriptor.properties, step: { type: "integer" } },
			writable: { ...descriptor.writable, step: { type: "integer" } },
		});
		await expect(previous.execute({ count: 8 }, {})).rejects.toMatchObject({ name: "AbortError" });
		await vi.waitFor(() =>
			expect(tool("update").inputSchema).toMatchObject({
				properties: { count: { type: "integer" }, step: { type: "integer" } },
			}),
		);
		const result = tool("update").execute({ step: 2 }, {});
		await Promise.resolve();
		fixture.reply({ result: { state: { count: 0, step: 2 } } });
		await expect(result).resolves.toEqual({ state: { count: 0, step: 2 } });
		await dispose(fixture.cleanup);
		expect(tools.size).toBe(0);
	});

	test("registers tools when metadata arrives after the view renders", async () => {
		const fixture = modelFixture(null);
		const widget = await instrument("source", load);
		const options = renderOptions(fixture.model);
		await Promise.resolve(widget.initialize?.(options));
		const cleanup = await Promise.resolve(widget.render?.(options));
		expect(tools.size).toBe(0);
		fixture.setMetadata(descriptor);
		await vi.waitFor(() => expect(tools.size).toBe(2));
		await dispose(cleanup);
		expect(tools.size).toBe(0);
	});

	test("preserves rendering and cleanup when exposure metadata is malformed", async () => {
		const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
		const cleanup = vi.fn();
		const render = vi.fn(() => cleanup);
		load.mockResolvedValue({ render });
		const fixture = modelFixture({ ...descriptor, writable: { count: "integer" } });
		const widget = await instrument("source", load);
		const options = renderOptions(fixture.model);
		await Promise.resolve(widget.initialize?.(options));
		const disposeView = await Promise.resolve(widget.render?.(options));
		expect(render).toHaveBeenCalledWith(options);
		expect(warning).toHaveBeenCalledOnce();
		expect(tools.size).toBe(0);
		await dispose(disposeView);
		expect(cleanup).toHaveBeenCalledOnce();
	});

	test("honors caller cancellation and bounds an unresponsive Python kernel", async () => {
		vi.useFakeTimers();
		const fixture = await mount();
		const controller = new AbortController();
		const cancelled = tool("read").execute({}, { signal: controller.signal });
		const rejected = expect(cancelled).rejects.toMatchObject({ name: "AbortError" });
		await Promise.resolve();
		controller.abort();
		await rejected;
		const timeout = tool("read").execute({}, {});
		const timedOut = expect(timeout).rejects.toThrow("timed out waiting for Python");
		await vi.runAllTimersAsync();
		await timedOut;
		expect(fixture.listeners.size).toBe(0);
		await dispose(fixture.cleanup);
	});

	test("continues queued requests after a synchronous comm send failure", async () => {
		const fixture = await mount();
		vi.mocked(fixture.model.send).mockImplementationOnce(() => {
			throw new Error("Comm send interrupted");
		});
		const failed = tool("update").execute({ count: 4 }, {});
		const read = tool("read").execute({}, {});
		await expect(failed).rejects.toThrow("Comm send interrupted");
		await vi.waitFor(() => expect(fixture.model.send).toHaveBeenCalledTimes(2));
		fixture.reply({ result: { state: { count: 0 } } }, 1);
		await expect(read).resolves.toEqual({ state: { count: 0 } });
		expect(fixture.listeners.size).toBe(0);
		await dispose(fixture.cleanup);
	});

	test("rejects malformed replies and correlates later replies with the queued request", async () => {
		const fixture = await mount();
		const failed = tool("update").execute({ count: 4 }, {});
		const rejected = expect(failed).rejects.toThrow("invalid AnyWidget WebMCP result");
		const finished = vi.fn();
		const read = tool("read").execute({}, {}).then(finished);
		await vi.waitFor(() => expect(fixture.model.send).toHaveBeenCalledOnce());
		fixture.reply({ result: { state: [] } });
		await rejected;
		await vi.waitFor(() => expect(fixture.model.send).toHaveBeenCalledTimes(2));
		fixture.reply({ result: { state: { count: 99 } } });
		await Promise.resolve();
		expect(finished).not.toHaveBeenCalled();
		fixture.reply({ result: { state: { count: 4 } } }, 1);
		await read;
		expect(finished).toHaveBeenCalledWith({ state: { count: 4 } });
		expect(fixture.listeners.size).toBe(0);
		await dispose(fixture.cleanup);
	});
	test("withdraws an instance when its native comm closes and retains its widget output", async () => {
		const cleanup = vi.fn();
		load.mockResolvedValue({
			render: ({ el }) => {
				el.textContent = "Counter";
				return cleanup;
			},
		});
		const fixture = await mount();
		const read = tool("read").execute({}, {});
		const rejected = expect(read).rejects.toMatchObject({ name: "AbortError" });
		await Promise.resolve();
		fixture.closeComm();
		await rejected;
		expect(tools.size).toBe(0);
		expect(fixture.options.el.textContent).toBe("Counter");
		expect(cleanup).not.toHaveBeenCalled();
		await dispose(fixture.cleanup);
		expect(cleanup).toHaveBeenCalledOnce();
	});
});
