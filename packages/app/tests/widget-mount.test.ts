// @vitest-environment jsdom

import { afterEach, describe, expect, test } from "vite-plus/test";

import { mountDetachedWidget } from "../src/widget-mount";

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

describe("widget mount", () => {
	afterEach(() => document.body.replaceChildren());

	test("keeps the render target detached until the widget is ready", async () => {
		const host = document.createElement("div");
		document.body.append(host);
		const ready = deferred();
		let target: HTMLElement | undefined;

		const mounting = mountDetachedWidget(host, async (element) => {
			target = element;
			const renderRoot = element.getRootNode();
			expect(renderRoot).toBe(element);
			expect(element.isConnected).toBe(false);
			expect(() => renderRoot.appendChild(document.createElement("style"))).not.toThrow();
			await ready.promise;
		});
		await Promise.resolve();

		expect(host.childNodes).toHaveLength(0);
		ready.resolve();
		await mounting;

		expect(host.firstChild).toBe(target);
		expect(target?.isConnected).toBe(true);
	});

	test("does not attach a widget cancelled after rendering", async () => {
		const host = document.createElement("div");
		document.body.append(host);
		const controller = new AbortController();
		const reason = new DOMException("Widget call cancelled", "AbortError");

		const mounting = mountDetachedWidget(
			host,
			async () => {
				controller.abort(reason);
			},
			controller.signal,
		);

		await expect(mounting).rejects.toBe(reason);
		expect(host.childNodes).toHaveLength(0);
	});
});
