import { describe, expect, test, vi } from "vite-plus/test";

import {
	BridgeModel,
	scopedModel,
	serializeCustom,
	serializeUpdate,
	type EventHandler,
	type ModelPayload,
	type State,
} from "../src/model";

function payload(state: State = {}): ModelPayload {
	return {
		modelId: "model-1",
		state,
	};
}

function modelRuntime() {
	let current!: BridgeModel;
	const enqueueUpdate = vi.fn();
	const enqueueCustom = vi.fn();
	return {
		runtime: {
			model: () => current,
			enqueueUpdate,
			enqueueCustom,
		},
		attach(model: BridgeModel) {
			current = model;
		},
		enqueueUpdate,
		enqueueCustom,
	};
}

describe("BridgeModel state synchronization", () => {
	test("round-trips binary dictionary keys through comm updates", () => {
		const harness = modelRuntime();
		const model = new BridgeModel(harness.runtime, payload(), vi.fn());
		harness.attach(model);
		const bytes = new DataView(new Uint8Array([1, 2, 3]).buffer);

		model.receive(
			{
				method: "update",
				state: { data: {} },
				buffer_paths: [["data", "__proto__"]],
			},
			[bytes],
		);
		model.set("data", model.get("data"));
		model.save_changes();

		expect(harness.enqueueUpdate).toHaveBeenCalledTimes(1);
		expect(serializeUpdate(harness.enqueueUpdate.mock.calls[0]![1])).toEqual({
			data: {
				method: "update",
				state: { data: {} },
				buffer_paths: [["data", "__proto__"]],
			},
			buffers: [new Uint8Array([1, 2, 3]).buffer],
		});
	});

	test("snapshots state when save_changes returns", () => {
		const harness = modelRuntime();
		const model = new BridgeModel(harness.runtime, payload(), vi.fn());
		harness.attach(model);
		const config = { value: 1 };
		const payloadBytes = new Uint8Array([1, 2, 3]);

		model.set("config", config);
		model.set("payload", payloadBytes);
		model.save_changes();
		config.value = 2;
		payloadBytes[0] = 9;

		expect(harness.enqueueUpdate).toHaveBeenCalledTimes(1);
		const state = harness.enqueueUpdate.mock.calls[0]?.[1];
		if (!state) throw new Error("Expected a queued state update");
		expect(serializeUpdate(state)).toEqual({
			data: {
				method: "update",
				state: { config: { value: 1 } },
				buffer_paths: [["payload"]],
			},
			buffers: [new Uint8Array([1, 2, 3]).buffer],
		});
	});

	test("snapshots custom content and buffers before enqueue", () => {
		const harness = modelRuntime();
		const model = new BridgeModel(harness.runtime, payload(), vi.fn());
		harness.attach(model);
		const content = { nested: { value: 1 } };
		const bytes = new Uint8Array([1, 2, 3]);

		model.send(content, undefined, [bytes]);
		content.nested.value = 2;
		bytes[0] = 9;

		expect(harness.enqueueCustom).toHaveBeenCalledWith(
			model,
			{ method: "custom", content: { nested: { value: 1 } } },
			[new Uint8Array([1, 2, 3]).buffer],
		);
	});

	test("rejects custom objects outside the runtime value contract", () => {
		expect(() => serializeCustom(new Date("2026-08-17T00:00:00Z"))).toThrow(
			"Widget value cannot cross the runtime boundary",
		);
	});

	test("rejects cyclic custom content", () => {
		interface CyclicContent {
			self?: CyclicContent;
		}
		const content: CyclicContent = {};
		content.self = content;

		expect(() => serializeCustom(content)).toThrow("Widget value cannot contain cycles");
	});

	test("rejects cyclic state updates", () => {
		interface CyclicState {
			self?: CyclicState;
		}
		const value: CyclicState = {};
		value.self = value;

		expect(() => serializeUpdate(new Map([["value", value]]))).toThrow(
			"Widget value cannot contain cycles",
		);
	});

	test("reports state that cannot cross the structured-clone boundary", () => {
		const harness = modelRuntime();
		const reportError = vi.fn();
		const model = new BridgeModel(harness.runtime, payload(), reportError);
		harness.attach(model);

		model.set("value", () => undefined);
		model.save_changes();

		expect(harness.enqueueUpdate).not.toHaveBeenCalled();
		expect(reportError).toHaveBeenCalledWith(expect.objectContaining({ name: "DataCloneError" }));
	});

	test("ignores model work after disposal", () => {
		const harness = modelRuntime();
		const model = new BridgeModel(harness.runtime, payload({ value: 0 }), vi.fn());
		harness.attach(model);

		model.dispose();
		model.set("value", 1);
		model.save_changes();
		model.send({ kind: "command" });

		expect(model.get("value")).toBe(0);
		expect(harness.enqueueUpdate).not.toHaveBeenCalled();
		expect(harness.enqueueCustom).not.toHaveBeenCalled();
	});
});

describe("BridgeModel events", () => {
	test.each(["off", "abort"] as const)(
		"keeps shared callbacks subscribed in other scopes after %s",
		(close) => {
			const harness = modelRuntime();
			const model = new BridgeModel(harness.runtime, payload({ value: 0 }), vi.fn());
			const first = new AbortController();
			const second = new AbortController();
			const firstModel = scopedModel(model, first.signal);
			const secondModel = scopedModel(model, second.signal);
			const callback = vi.fn();
			firstModel.on("change:value", callback);
			secondModel.on("change:value", callback);

			model.receive({ method: "update", state: { value: 1 } }, []);
			expect(callback).toHaveBeenCalledTimes(2);
			if (close === "off") firstModel.off("change:value", callback);
			else first.abort();
			model.receive({ method: "update", state: { value: 2 } }, []);

			expect(callback).toHaveBeenCalledTimes(3);
			first.abort();
			second.abort();
		},
	);

	test("retains buffered custom messages after the subscribing scope aborts", () => {
		const harness = modelRuntime();
		const model = new BridgeModel(harness.runtime, payload(), vi.fn());
		const controller = new AbortController();
		const scoped = scopedModel(model, controller.signal);
		const received: unknown[] = [];
		model.receive({ method: "custom", content: { index: 1 } }, []);
		model.receive({ method: "custom", content: { index: 2 } }, []);

		scoped.on("msg:custom", (content) => {
			received.push(content);
			controller.abort();
		});
		const next = vi.fn();
		model.on("msg:custom", next);

		expect(received).toEqual([{ index: 1 }]);
		expect(next).toHaveBeenCalledExactlyOnceWith({ index: 2 }, []);
	});

	test("keeps buffered widget messages behind transient command responses", () => {
		const harness = modelRuntime();
		const model = new BridgeModel(harness.runtime, payload(), vi.fn());
		harness.attach(model);
		const responses: unknown[] = [];
		const received: unknown[] = [];

		model.receive({ method: "custom", content: { source: "outer" } }, []);
		const removeResponseHandler = model.onCommandResponse("command-1", (content) => {
			responses.push(content.response);
		});
		model.receive(
			{
				method: "custom",
				content: {
					id: "command-1",
					kind: "anywidget-command-response",
					response: "ready",
				},
			},
			[],
		);
		removeResponseHandler();
		model.on("msg:custom", (content) => received.push(content));

		expect(responses).toEqual(["ready"]);
		expect(received).toEqual([{ source: "outer" }]);
	});

	test("drops a reserved command response after its private handler is removed", () => {
		const harness = modelRuntime();
		const model = new BridgeModel(harness.runtime, payload(), vi.fn());
		harness.attach(model);
		const responses: unknown[] = [];
		const received: unknown[] = [];

		model.receive({ method: "custom", content: { source: "before" } }, []);
		const removeResponseHandler = model.onCommandResponse("command-1", (content) => {
			responses.push(content.response);
		});
		removeResponseHandler();
		model.receive(
			{
				method: "custom",
				content: {
					id: "command-1",
					kind: "anywidget-command-response",
					response: "late",
				},
			},
			[],
		);
		model.on("msg:custom", (content) => received.push(content));
		model.receive({ method: "custom", content: { source: "after" } }, []);

		expect(responses).toEqual([]);
		expect(received).toEqual([{ source: "before" }, { source: "after" }]);
	});

	test("retains startup messages for the first listener and drops unobserved live messages", () => {
		const harness = modelRuntime();
		const model = new BridgeModel(harness.runtime, payload(), vi.fn());
		harness.attach(model);
		const received: unknown[] = [];
		const listener: EventHandler = (content) => received.push(content);
		for (let index = 0; index < 150; index += 1) {
			model.receive({ method: "custom", content: index }, []);
		}
		model.finishInitialization();
		for (let index = 150; index < 300; index += 1) {
			model.receive({ method: "custom", content: index }, []);
		}
		model.on("msg:custom", listener);
		expect(received).toEqual(Array.from({ length: 150 }, (_, index) => index));

		model.off("msg:custom", listener);
		model.receive({ method: "custom", content: "unobserved" }, []);
		model.on("msg:custom", listener);
		model.receive({ method: "custom", content: "observed" }, []);
		expect(received.at(-1)).toBe("observed");
		expect(received).toHaveLength(151);
	});

	test("dispatches space-delimited change subscriptions with no arguments", () => {
		const harness = modelRuntime();
		const model = new BridgeModel(harness.runtime, payload({ a: 0, b: 0 }), vi.fn());
		harness.attach(model);
		const callback = vi.fn();

		model.on("change:a change:b", callback);
		model.set("a", 1);
		model.receive({ method: "update", state: { b: 1 } }, []);

		expect(callback.mock.calls).toEqual([[], []]);

		model.off("change:a", callback);
		model.set("a", 2);
		model.receive({ method: "update", state: { b: 2 } }, []);

		expect(callback.mock.calls).toEqual([[], [], []]);
	});

	test("dispatches one generic change for each state operation", () => {
		const harness = modelRuntime();
		const model = new BridgeModel(harness.runtime, payload({ a: 0, b: 0 }), vi.fn());
		harness.attach(model);
		const callback = vi.fn();
		model.on("change", callback);

		model.receive({ method: "update", state: { a: 1, b: 1 } }, []);
		model.set("a", 2);
		model.receive({ method: "update", state: { a: 2, b: 1 } }, []);

		expect(callback.mock.calls).toEqual([[], []]);
	});

	test("scoped models remove one split event and clear the rest on abort", () => {
		const harness = modelRuntime();
		const model = new BridgeModel(harness.runtime, payload({ a: 0, b: 0 }), vi.fn());
		harness.attach(model);
		const controller = new AbortController();
		const scoped = scopedModel(model, controller.signal);
		const callback = vi.fn();

		scoped.on("change:a change:b", callback);
		scoped.off("change:a", callback);
		model.receive({ method: "update", state: { a: 1, b: 1 } }, []);

		expect(callback.mock.calls).toEqual([[]]);

		controller.abort();
		model.receive({ method: "update", state: { b: 2 } }, []);

		expect(callback).toHaveBeenCalledTimes(1);
	});

	test.each(["before", "after"] as const)(
		"blocks every scoped operation when aborted %s construction",
		async (timing) => {
			const harness = modelRuntime();
			const model = new BridgeModel(harness.runtime, payload({ value: 0 }), vi.fn());
			harness.attach(model);
			const controller = new AbortController();
			if (timing === "before") controller.abort();
			const scoped = scopedModel(model, controller.signal);
			if (timing === "after") controller.abort();
			const callback = vi.fn();

			scoped.on("change:value", callback);
			scoped.set("value", 1);
			scoped.save_changes();
			scoped.send({ kind: "command" });
			scoped.off();

			expect(scoped.get("value")).toBeUndefined();
			expect(model.get("value")).toBe(0);
			expect(harness.enqueueUpdate).not.toHaveBeenCalled();
			expect(harness.enqueueCustom).not.toHaveBeenCalled();
			await expect(scoped.widget_manager.get_model("model-1")).rejects.toMatchObject({
				name: "AbortError",
			});

			model.receive({ method: "update", state: { value: 2 } }, []);
			expect(callback).not.toHaveBeenCalled();
		},
	);
});
