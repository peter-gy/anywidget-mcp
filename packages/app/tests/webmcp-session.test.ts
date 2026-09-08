// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, test, vi } from "vite-plus/test";
import type { Host, WidgetDefinition } from "../src/binding";
import type { AnyModel, EventHandler } from "../src/model";
import { isRecord, type RuntimeRecord, type WidgetValue } from "../src/runtime-value";
import { instrument, session } from "../src/webmcp";
import type { WebMCPTool } from "../src/_webmcp/registry";

const descriptor = {
	id: "counter1",
	title: "Counter",
	description: "An interactive counter.",
	properties: { value: { type: "integer" } },
	writable: { value: { type: "integer" } },
};
const creation = {
	id: "creation1",
	name: "create_counter",
	title: "Create counter",
	description: "Create a counter.",
	inputSchema: { type: "object", properties: { start: { type: "integer", default: 0 } } },
};

function modelFixture(state: Record<string, WidgetValue>) {
	const events = new Map<string, Set<EventHandler>>();
	const model: AnyModel = {
		get: (name) => state[name],
		set: (name, value) => {
			state[name] = value;
		},
		save_changes: vi.fn(),
		send: vi.fn(),
		on(name, handler) {
			const listeners = events.get(name) ?? new Set();
			listeners.add(handler);
			events.set(name, listeners);
		},
		off(name, handler) {
			if (name && handler) {
				events.get(name)?.delete(handler);
				if (!events.get(name)?.size) events.delete(name);
			}
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
		events,
		change(name: string, value: WidgetValue) {
			model.set(name, value);
			for (const handler of Array.from(events.get(`change:${name}`) ?? [])) handler();
		},
		reply(result: RuntimeRecord, index = 0) {
			const message = vi.mocked(model.send).mock.calls[index]?.[0];
			if (!isRecord(message)) throw new Error("Missing request");
			for (const handler of Array.from(events.get("msg:custom") ?? []))
				handler({ kind: "anywidget-webmcp-result", id: message.id, ...result });
		},
	};
}
function sessionFixture() {
	return modelFixture({
		_webmcp_active: true,
		_webmcp_catalog: [creation],
		_webmcp_widgets: [],
		_webmcp_created: [],
	});
}
function deferred<T>() {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>((done) => {
		resolve = done;
	});
	return { promise, resolve };
}

function kernelFixture() {
	const signal = () => {
		const listeners = new Set<(sender: WidgetValue, value: WidgetValue) => void>();
		return {
			listeners,
			connect(slot: (sender: WidgetValue, value: WidgetValue) => void) {
				listeners.add(slot);
				return true;
			},
			disconnect(slot: (sender: WidgetValue, value: WidgetValue) => void) {
				return listeners.delete(slot);
			},
			emit(value?: WidgetValue) {
				for (const slot of Array.from(listeners)) slot(undefined, value);
			},
		};
	};
	return { disposed: signal(), statusChanged: signal(), status: "idle", isDisposed: false };
}

describe("WebMCP session", () => {
	const tools = new Map<string, WebMCPTool>();
	const views: AbortController[] = [];
	const registerTool = vi.fn((tool: WebMCPTool, { signal }: { signal: AbortSignal }) => {
		signal.throwIfAborted();
		if (tools.has(tool.name)) throw new Error(`Duplicate tool ${tool.name}`);
		tools.set(tool.name, tool);
		signal.addEventListener("abort", () => tools.delete(tool.name), { once: true });
		return Promise.resolve();
	});
	beforeEach(() => {
		Object.defineProperty(document, "modelContext", {
			configurable: true,
			value: { registerTool },
		});
	});
	afterEach(() => {
		for (const view of views) view.abort();
		views.length = 0;
		tools.clear();
		Reflect.deleteProperty(document, "modelContext");
		vi.restoreAllMocks();
		vi.useRealTimers();
		registerTool.mockClear();
	});
	function mount(model: AnyModel, host: Host, definition: WidgetDefinition = session) {
		const controller = new AbortController();
		views.push(controller);
		const options = {
			model,
			host,
			signal: controller.signal,
			el: document.createElement("div"),
			experimental: {
				async invoke() {
					throw new Error("This WebMCP fixture uses model.send for requests");
				},
			},
		};
		return { controller, options, ready: Promise.resolve(definition.render?.(options)) };
	}
	function getTool(name: string) {
		const tool = tools.get(name);
		if (!tool) throw new Error(`Missing tool ${name}`);
		return tool;
	}
	function hostFixture(child = modelFixture({ _webmcp: descriptor })) {
		const getModel = vi.fn(() => Promise.resolve(child.model));
		const getWidget = vi.fn(() => Promise.resolve());
		const render = vi.fn(({ el }: { el: HTMLElement; signal?: AbortSignal }) => {
			el.textContent = "Counter: 7";
			return Promise.resolve();
		});
		return {
			child,
			render,
			getModel,
			getWidget,
			host: {
				async getModel<T extends object>() {
					// SAFETY: The fixture response is the model selected by the test.
					return (await getModel()) as AnyModel<T>;
				},
				async getWidget<T>() {
					await getWidget();
					// SAFETY: The fixture renders a child whose initializer returns no exports.
					return { exports: undefined as T, render };
				},
			},
		};
	}

	test("publishes typed creation tools and resolves after the created widget renders and registers", async () => {
		const fixture = sessionFixture();
		const host = hostFixture();
		const rendered = deferred<void>();
		host.render.mockImplementationOnce(async ({ el }) => {
			await rendered.promise;
			el.textContent = "Counter: 7";
		});
		const view = mount(fixture.model, host.host);
		await view.ready;
		expect(getTool("create_counter").inputSchema).toEqual(creation.inputSchema);
		const finished = vi.fn();
		const result = getTool("create_counter").execute({ start: 7 }, {}).then(finished);
		await vi.waitFor(() => expect(fixture.model.send).toHaveBeenCalledOnce());
		expect(fixture.model.send).toHaveBeenCalledWith({
			kind: "anywidget-webmcp",
			id: expect.any(String),
			operation: "create",
			target: "creation1",
			arguments: { start: 7 },
		});
		fixture.change("_webmcp_widgets", ["anywidget:counter1"]);
		fixture.change("_webmcp_created", ["anywidget:counter1"]);
		const response = {
			widget_id: "counter1",
			ref: "anywidget:counter1",
			state: { value: 7 },
			tools: { read: "anywidget_counter1_read", update: "anywidget_counter1_update" },
		};
		fixture.reply({ result: response });
		await vi.waitFor(() => expect(host.render).toHaveBeenCalledOnce());
		expect(tools.has("anywidget_counter1_read")).toBe(true);
		expect(finished).not.toHaveBeenCalled();
		rendered.resolve();
		await result;
		expect(finished).toHaveBeenCalledWith(response);
		expect(view.options.el.textContent).toContain("Counter: 7");
	});

	test("exposes discovered models and shares their tools with ordinary widget views", async () => {
		const fixture = sessionFixture();
		fixture.change("_webmcp_widgets", ["anywidget:counter1"]);
		const host = hostFixture();
		const view = mount(fixture.model, host.host);
		await vi.waitFor(() => expect(tools.size).toBe(3));
		expect(host.render).not.toHaveBeenCalled();
		const widget = await instrument("counter source", () =>
			Promise.resolve({ render: () => undefined }),
		);
		const ordinary = mount(host.child.model, host.host, widget);
		await ordinary.ready;
		expect(registerTool).toHaveBeenCalledTimes(3);
		view.controller.abort();
		await vi.waitFor(() => expect(tools.size).toBe(2));
		const result = getTool("anywidget_counter1_read").execute({}, {});
		await vi.waitFor(() => expect(host.child.model.send).toHaveBeenCalledOnce());
		host.child.reply({ result: { state: { value: 7 } } });
		await expect(result).resolves.toEqual({ state: { value: 7 } });
		ordinary.controller.abort();
		expect(tools.size).toBe(0);
		expect(host.child.events.size).toBe(0);
	});

	test("shares creation tools across session views and retains a working owner", async () => {
		const fixture = sessionFixture();
		const host = hostFixture();
		const first = mount(fixture.model, host.host);
		const second = mount(fixture.model, host.host);
		await Promise.all([first.ready, second.ready]);
		expect(registerTool).toHaveBeenCalledOnce();
		first.controller.abort();
		await vi.waitFor(() => expect(tools.size).toBe(1));
		const result = getTool("create_counter").execute({}, {});
		await vi.waitFor(() => expect(fixture.model.send).toHaveBeenCalledOnce());
		fixture.reply({
			result: {
				widget_id: "counter1",
				ref: "anywidget:counter1",
				state: { value: 7 },
				tools: { read: "anywidget_counter1_read" },
			},
		});
		await result;
		expect(second.options.el.textContent).toContain("Counter: 7");
		second.controller.abort();
		expect(tools.size).toBe(0);
		expect(fixture.events.size).toBe(0);
	});

	test("updates names and writable schemas across scoped views", async () => {
		const fixture = sessionFixture();
		fixture.change("_webmcp_widgets", ["anywidget:counter1"]);
		const host = hostFixture();
		mount(fixture.model, host.host);
		const widget = await instrument("source", () => Promise.resolve({}));
		await mount(host.child.model, host.host, widget).ready;
		await vi.waitFor(() => expect(tools.size).toBe(3));
		host.child.change("_webmcp", { ...descriptor, name: "review", writable: {} });
		await vi.waitFor(() =>
			expect([...tools.keys()].sort()).toEqual(["create_counter", "review_counter1_read"]),
		);
	});

	test("withdraws tools on disable while retaining rendered created widgets", async () => {
		const fixture = sessionFixture();
		fixture.change("_webmcp_widgets", ["anywidget:counter1"]);
		fixture.change("_webmcp_created", ["anywidget:counter1"]);
		const host = hostFixture();
		const view = mount(fixture.model, host.host);
		await vi.waitFor(() => expect(view.options.el.textContent).toContain("Counter: 7"));
		fixture.change("_webmcp_active", false);
		expect(tools.size).toBe(0);
		expect(view.options.el.textContent).toContain("Counter: 7");
		fixture.change("_webmcp_active", true);
		await vi.waitFor(() => expect(tools.size).toBe(3));
		expect(host.render).toHaveBeenCalledOnce();
	});

	test("aborts a pending creation and releases listeners when its view closes", async () => {
		const fixture = sessionFixture();
		const host = hostFixture();
		const view = mount(fixture.model, host.host);
		await view.ready;
		const result = getTool("create_counter").execute({}, {});
		const rejected = expect(result).rejects.toMatchObject({ name: "AbortError" });
		await vi.waitFor(() => expect(fixture.model.send).toHaveBeenCalledOnce());
		view.controller.abort();
		await rejected;
		expect(fixture.events.size).toBe(0);
		expect(tools.size).toBe(0);
	});

	test("keeps creation tools withdrawn when disable races a completed Python reply", async () => {
		const fixture = sessionFixture();
		const host = hostFixture();
		const view = mount(fixture.model, host.host);
		await view.ready;
		const result = getTool("create_counter").execute({}, {});
		const rejected = expect(result).rejects.toMatchObject({ name: "AbortError" });
		await vi.waitFor(() => expect(fixture.model.send).toHaveBeenCalledOnce());
		fixture.change("_webmcp_widgets", ["anywidget:counter1"]);
		fixture.change("_webmcp_created", ["anywidget:counter1"]);
		fixture.reply({
			result: {
				widget_id: "counter1",
				ref: "anywidget:counter1",
				state: { value: 7 },
				tools: { read: "anywidget_counter1_read" },
			},
		});
		fixture.change("_webmcp_active", false);
		await rejected;
		await vi.waitFor(() => expect(view.options.el.textContent).toContain("Counter: 7"));
		expect(tools.size).toBe(0);
		expect(view.options.el.textContent).toContain("WebMCP disabled");
	});

	test("rejects a creation when the resulting instance tools cannot register", async () => {
		const fixture = sessionFixture();
		const host = hostFixture();
		mount(fixture.model, host.host);
		await vi.waitFor(() => expect(tools.size).toBe(1));
		vi.spyOn(console, "warn").mockImplementation(() => {});
		registerTool.mockImplementationOnce(() => Promise.reject(new Error("Tool permission denied")));
		const result = getTool("create_counter").execute({}, {});
		const rejected = expect(result).rejects.toThrow("Tool permission denied");
		await vi.waitFor(() => expect(fixture.model.send).toHaveBeenCalledOnce());
		fixture.reply({
			result: {
				widget_id: "counter1",
				ref: "anywidget:counter1",
				state: { value: 7 },
				tools: { read: "anywidget_counter1_read" },
			},
		});
		await rejected;
	});
	test("waits for browser registration before completing creation", async () => {
		const fixture = sessionFixture();
		const host = hostFixture();
		await mount(fixture.model, host.host).ready;
		const registered = deferred<void>();
		const register = registerTool.getMockImplementation();
		if (!register) throw new Error("Missing browser registry");
		registerTool.mockImplementationOnce(async (tool, options) => {
			await registered.promise;
			return register(tool, options);
		});
		const finished = vi.fn();
		const result = getTool("create_counter").execute({}, {}).then(finished);
		await vi.waitFor(() => expect(fixture.model.send).toHaveBeenCalledOnce());
		fixture.reply({
			result: {
				widget_id: "counter1",
				ref: "anywidget:counter1",
				state: { value: 7 },
				tools: { read: "anywidget_counter1_read" },
			},
		});
		await vi.waitFor(() => expect(registerTool).toHaveBeenCalledTimes(3));
		expect(finished).not.toHaveBeenCalled();
		registered.resolve();
		await result;
		expect(tools.has("anywidget_counter1_read")).toBe(true);
		expect(host.render).toHaveBeenCalledOnce();
	});

	test.each(["model", "widget", "render", "registration"] as const)(
		"bounds creation waiting for %s and withdraws timed-out work",
		async (stage) => {
			vi.useFakeTimers();
			vi.spyOn(console, "warn").mockImplementation(() => {});
			const fixture = sessionFixture();
			const host = hostFixture();
			const view = mount(fixture.model, host.host);
			await view.ready;
			const delayed = deferred<void>();
			if (stage === "model") {
				host.getModel.mockImplementationOnce(async () => {
					await delayed.promise;
					return host.child.model;
				});
			} else if (stage === "widget") {
				host.getWidget.mockReturnValueOnce(delayed.promise);
			} else if (stage === "render") {
				host.render.mockImplementationOnce(async ({ el }) => {
					await delayed.promise;
					el.textContent = "Late counter";
				});
			} else {
				const register = registerTool.getMockImplementation();
				if (!register) throw new Error("Missing browser registry");
				registerTool.mockImplementationOnce(async (tool, options) => {
					await delayed.promise;
					return register(tool, options);
				});
			}
			const rejected = vi.fn();
			const result = getTool("create_counter").execute({}, {}).catch(rejected);
			await vi.advanceTimersByTimeAsync(0);
			fixture.change("_webmcp_widgets", ["anywidget:counter1"]);
			fixture.change("_webmcp_created", ["anywidget:counter1"]);
			fixture.reply({
				result: {
					widget_id: "counter1",
					ref: "anywidget:counter1",
					state: { value: 7 },
					tools: { read: "anywidget_counter1_read" },
				},
			});
			await vi.advanceTimersByTimeAsync(10_000);
			expect(rejected).toHaveBeenCalledWith(
				expect.objectContaining({ message: expect.stringMatching(/timed out/i) }),
			);
			await result;
			expect([...tools.keys()]).toEqual(["create_counter"]);
			delayed.resolve();
			await vi.advanceTimersByTimeAsync(0);
			expect([...tools.keys()]).toEqual(["create_counter"]);
			expect(view.options.el.textContent).not.toContain("Late counter");
			if (stage === "model" || stage === "widget") expect(host.render).not.toHaveBeenCalled();
			view.controller.abort();
			expect(tools.size).toBe(0);
			expect(view.options.el.textContent).toBe("");
			expect(host.child.events.size).toBe(0);
		},
	);

	test("retries failed creation tool registration when another view opens", async () => {
		const fixture = sessionFixture();
		const host = hostFixture();
		vi.spyOn(console, "warn").mockImplementation(() => {});
		registerTool.mockRejectedValueOnce(new Error("Tool registration interrupted"));
		const first = mount(fixture.model, host.host);
		await vi.waitFor(() =>
			expect(first.options.el.textContent).toContain("Tool registration interrupted"),
		);
		const second = mount(fixture.model, host.host);
		await vi.waitFor(() => expect(second.options.el.textContent).toBe("WebMCP ready"));
		const result = getTool("create_counter").execute({}, {});
		const rejected = expect(result).rejects.toThrow("Invalid starting value");
		await vi.waitFor(() => expect(fixture.model.send).toHaveBeenCalledOnce());
		fixture.reply({ error: "Invalid starting value" });
		await rejected;
	});

	test("retries failed creation tool registration when the catalog is refreshed", async () => {
		const fixture = sessionFixture();
		const host = hostFixture();
		vi.spyOn(console, "warn").mockImplementation(() => {});
		registerTool.mockRejectedValueOnce(new Error("Tool registration interrupted"));
		const view = mount(fixture.model, host.host);
		await vi.waitFor(() =>
			expect(view.options.el.textContent).toContain("Tool registration interrupted"),
		);
		fixture.change("_webmcp_catalog", [creation]);
		await vi.waitFor(() => expect(view.options.el.textContent).toBe("WebMCP ready"));
		expect(tools.has("create_counter")).toBe(true);
	});

	test("reconciles exposure changes while a created model is resolving", async () => {
		const fixture = sessionFixture();
		fixture.change("_webmcp_widgets", ["anywidget:counter1"]);
		fixture.change("_webmcp_created", ["anywidget:counter1"]);
		const host = hostFixture();
		const resolved = deferred<AnyModel>();
		host.getModel.mockReturnValue(resolved.promise);
		const view = mount(fixture.model, host.host);
		await view.ready;
		fixture.change("_webmcp_active", false);
		fixture.change("_webmcp_active", true);
		resolved.resolve(host.child.model);
		await vi.waitFor(() => expect(tools.size).toBe(3));
		expect(host.render).toHaveBeenCalledOnce();
		view.controller.abort();
		expect(tools.size).toBe(0);
		expect(host.child.events.size).toBe(0);
	});
	test("restores exposure after disabling a pending browser registration", async () => {
		const fixture = sessionFixture();
		fixture.change("_webmcp_widgets", ["anywidget:counter1"]);
		fixture.change("_webmcp_created", ["anywidget:counter1"]);
		const host = hostFixture();
		const registered = deferred<void>();
		const register = registerTool.getMockImplementation();
		if (!register) throw new Error("Missing browser registry");
		registerTool.mockImplementationOnce(register).mockImplementationOnce(async (tool, options) => {
			await registered.promise;
			return register(tool, options);
		});
		const warning = vi.spyOn(console, "warn").mockImplementation(() => {});
		const view = mount(fixture.model, host.host);
		await vi.waitFor(() => expect(registerTool).toHaveBeenCalledTimes(3));
		expect(view.options.el.textContent).toContain("Preparing WebMCP");
		fixture.change("_webmcp_active", false);
		fixture.change("_webmcp_active", true);
		await vi.waitFor(() => expect(tools.size).toBe(3));
		registered.resolve();
		await vi.waitFor(() => expect(view.options.el.textContent).toContain("WebMCP ready"));
		expect(warning).not.toHaveBeenCalled();
		view.controller.abort();
		expect(tools.size).toBe(0);
	});

	test("completes creation with the current schemas when registration is replaced", async () => {
		const fixture = sessionFixture();
		const host = hostFixture();
		await mount(fixture.model, host.host).ready;
		const registered = deferred<void>();
		const register = registerTool.getMockImplementation();
		if (!register) throw new Error("Missing browser registry");
		registerTool.mockImplementationOnce(async (tool, options) => {
			await registered.promise;
			return register(tool, options);
		});
		const response = {
			widget_id: "counter1",
			ref: "anywidget:counter1",
			state: { value: 7 },
			tools: { read: "anywidget_counter1_read" },
		};
		const result = getTool("create_counter")
			.execute({}, {})
			.then(
				(value) => ({ value }),
				(error: WidgetValue) => ({ error }),
			);
		await vi.waitFor(() => expect(fixture.model.send).toHaveBeenCalledOnce());
		fixture.reply({ result: response });
		await vi.waitFor(() => expect(registerTool).toHaveBeenCalledTimes(3));
		host.child.change("_webmcp", { ...descriptor, writable: {} });
		await expect(result).resolves.toEqual({ value: response });
		expect([...tools.keys()].sort()).toEqual(["anywidget_counter1_read", "create_counter"]);
		registered.resolve();
	});
	test("withdraws tools and pending requests when the native kernel is disposed", async () => {
		const fixture = sessionFixture();
		fixture.change("_webmcp_widgets", ["anywidget:counter1"]);
		fixture.change("_webmcp_created", ["anywidget:counter1"]);
		const host = hostFixture();
		const kernel = kernelFixture();
		const sessionManager = { ...fixture.model.widget_manager, kernel };
		const childManager = { ...host.child.model.widget_manager, kernel };
		fixture.model.widget_manager = sessionManager;
		host.child.model.widget_manager = childManager;
		const view = mount(fixture.model, host.host);
		await vi.waitFor(() => expect(view.options.el.textContent).toContain("Counter: 7"));
		expect(tools.size).toBe(3);
		const pending = getTool("create_counter").execute({}, {});
		const rejected = expect(pending).rejects.toMatchObject({ name: "AbortError" });
		await vi.waitFor(() => expect(fixture.model.send).toHaveBeenCalledOnce());
		kernel.disposed.emit();
		await rejected;
		expect(tools.size).toBe(0);
		expect(view.options.el.textContent).toContain("WebMCP disconnected");
		expect(view.options.el.textContent).toContain("Counter: 7");
		expect(host.render.mock.calls[0]?.[0].signal?.aborted).toBe(false);
		expect(fixture.events.has("msg:custom")).toBe(false);
		expect(kernel.disposed.listeners.size).toBe(0);
		expect(kernel.statusChanged.listeners.size).toBe(0);
	});

	test("withdraws native registrations on kernel restart and releases signal subscriptions", async () => {
		const fixture = sessionFixture();
		const host = hostFixture();
		const kernel = kernelFixture();
		const manager = { ...fixture.model.widget_manager, kernel };
		fixture.model.widget_manager = manager;
		const view = mount(fixture.model, host.host);
		await view.ready;
		expect(tools.size).toBe(1);
		kernel.statusChanged.emit("busy");
		expect(tools.size).toBe(1);
		kernel.statusChanged.emit("restarting");
		expect(tools.size).toBe(0);
		expect(view.options.el.textContent).toBe("WebMCP disconnected");
		expect(kernel.statusChanged.listeners.size).toBe(0);
		expect(kernel.disposed.listeners.size).toBe(0);
		view.controller.abort();
		expect(fixture.events.size).toBe(0);
	});

	test("detaches native kernel subscriptions when the session view closes", async () => {
		const fixture = sessionFixture();
		const host = hostFixture();
		const kernel = kernelFixture();
		const manager = { ...fixture.model.widget_manager, kernel };
		fixture.model.widget_manager = manager;
		const view = mount(fixture.model, host.host);
		await view.ready;
		expect(kernel.disposed.listeners.size).toBe(1);
		view.controller.abort();
		expect(kernel.statusChanged.listeners.size).toBe(0);
		expect(kernel.disposed.listeners.size).toBe(0);
		expect(fixture.events.size).toBe(0);
	});
});
