// @vitest-environment jsdom

import { execFile } from "node:child_process";
import { randomUUID } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { promisify } from "node:util";

import { afterEach, describe, expect, test, vi } from "vite-plus/test";

import { instrumentInlineModule, loadModule, type WidgetModule } from "../src/module-loader";

const receivers = globalThis as unknown as Record<string, unknown>;
const executeFile = promisify(execFile);

afterEach(() => vi.restoreAllMocks());

async function executeInlineModule(source: string, inspect: string): Promise<unknown> {
	const receiverName = `__anywidget_mcp_test_${randomUUID().replaceAll("-", "")}`;
	const instrumented = instrumentInlineModule(source, receiverName);
	const directory = await mkdtemp(join(tmpdir(), "anywidget-mcp-module-"));
	const path = join(directory, "widget.mjs");
	const report = `globalThis[${JSON.stringify(receiverName)}] = (module) => console.log(${JSON.stringify("anywidget-result:")} + JSON.stringify((${inspect})(module)));`;
	try {
		await writeFile(path, `${report}\n${instrumented.code}`, "utf8");
		const { stdout } = await executeFile(process.execPath, [path]);
		const result = stdout.split("\n").find((line) => line.startsWith("anywidget-result:"));
		if (!result) throw new Error("Instrumented module did not publish its exports");
		return JSON.parse(result.slice("anywidget-result:".length)) as unknown;
	} finally {
		await rm(directory, { recursive: true, force: true });
	}
}

function receiverFrom(script: HTMLScriptElement): (module: WidgetModule) => void {
	const receiverName = receiverNameFrom(script);
	const receiver = receivers[receiverName];
	if (typeof receiver !== "function") throw new Error("Module receiver is not registered");
	return receiver as (module: WidgetModule) => void;
}

function receiverNameFrom(script: HTMLScriptElement): string {
	const match = /globalThis\[("[^"\\]*(?:\\.[^"\\]*)*")\]/.exec(script.textContent ?? "");
	if (!match) throw new Error("Missing module receiver");
	return JSON.parse(match[1] as string) as string;
}

describe("inline anywidget modules", () => {
	test("captures a default expression", async () => {
		const value = await executeInlineModule(
			`export default { marker: "expression" };`,
			"module => module.default",
		);

		expect(value).toEqual({ marker: "expression" });
	});

	test("captures anonymous default functions and classes", async () => {
		const functionValue = await executeInlineModule(
			`export default function () { return "function"; }`,
			"module => module.default()",
		);
		const classValue = await executeInlineModule(
			`export default class { marker = "class"; }`,
			"module => new module.default().marker",
		);

		expect(functionValue).toBe("function");
		expect(classValue).toBe("class");
	});

	test("keeps a statement after an anonymous default declaration separate", async () => {
		const value = await executeInlineModule(
			`
				globalThis.__anywidgetContinuation = 0;
				export default function () { return "default"; }
				(() => { globalThis.__anywidgetContinuation += 1; })();
			`,
			"module => ({ exported: module.default(), continuation: globalThis.__anywidgetContinuation })",
		);

		expect(value).toEqual({ exported: "default", continuation: 1 });
	});

	test("captures a named default declaration", async () => {
		const value = await executeInlineModule(
			`
				const beforeDeclaration = WidgetDefinition();
				export default function WidgetDefinition() { return "named"; }
				WidgetDefinition.beforeDeclaration = beforeDeclaration;
			`,
			"module => ({ beforeDeclaration: module.default.beforeDeclaration, exported: module.default() })",
		);

		expect(value).toEqual({ beforeDeclaration: "named", exported: "named" });
	});

	test("captures a named render export", async () => {
		const value = await executeInlineModule(
			`export function render() { return "rendered"; }`,
			"module => module.render()",
		);

		expect(value).toBe("rendered");
	});

	test("resolves local export aliases", async () => {
		const value = await executeInlineModule(
			`
				const definition = { marker: "aliased default" };
				function draw() { return "aliased render"; }
				export { definition as default, draw as render };
			`,
			"module => ({ marker: module.default.marker, rendered: module.render() })",
		);

		expect(value).toEqual({ marker: "aliased default", rendered: "aliased render" });
	});
});

describe("namespace import fallback", () => {
	test.each([
		`export * from "./widget.js";`,
		`export { widget as default } from "./widget.js";`,
		`export { draw as render } from "./widget.js";`,
	])("loads %s through a Blob URL and revokes it", async (source) => {
		const url = `data:text/javascript,${encodeURIComponent(`
			export const render = () => "fallback";
			export default { marker: "namespace" };
		`)}#${randomUUID()}`;
		const createUrl = vi.spyOn(URL, "createObjectURL").mockReturnValue(url);
		const revokeUrl = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);

		const module = await loadModule(source);

		expect((module.render as () => string)()).toBe("fallback");
		expect(module.default).toEqual({ marker: "namespace" });
		expect(createUrl).toHaveBeenCalledOnce();
		expect(revokeUrl).toHaveBeenCalledWith(url);
	});

	test("revokes the Blob URL when namespace evaluation fails", async () => {
		const url = `data:text/javascript,${encodeURIComponent(`throw new Error("fallback failed")`)}#${randomUUID()}`;
		vi.spyOn(URL, "createObjectURL").mockReturnValue(url);
		const revokeUrl = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined);

		await expect(loadModule(`export * from "./widget.js";`)).rejects.toThrow("fallback failed");

		expect(revokeUrl).toHaveBeenCalledWith(url);
	});
});

describe("inline module script lifecycle", () => {
	test("treats an HTTP label as inline source", async () => {
		const append = vi.spyOn(document.head, "append").mockImplementation((...nodes) => {
			const script = nodes[0] as HTMLScriptElement;
			queueMicrotask(() => receiverFrom(script)({ default: { marker: "inline" } }));
		});

		await expect(loadModule(`http: {}\nexport default {};`)).resolves.toEqual({
			default: { marker: "inline" },
		});
		expect(append).toHaveBeenCalledOnce();
	});

	test("resolves the module and removes its script", async () => {
		let script: HTMLScriptElement | undefined;
		let receiverName: string | undefined;
		const append = document.head.append.bind(document.head);
		vi.spyOn(document.head, "append").mockImplementation((...nodes) => {
			script = nodes[0] as HTMLScriptElement;
			receiverName = receiverNameFrom(script);
			append(...nodes);
			queueMicrotask(() =>
				receiverFrom(script as HTMLScriptElement)({ default: { marker: true } }),
			);
		});

		await expect(loadModule(`export default { marker: true };`)).resolves.toEqual({
			default: { marker: true },
		});
		expect(script?.isConnected).toBe(false);
		expect(receivers[receiverName as string]).toBeUndefined();
	});

	test("rejects execution errors and removes its script", async () => {
		let script: HTMLScriptElement | undefined;
		let receiverName: string | undefined;
		const append = document.head.append.bind(document.head);
		vi.spyOn(document.head, "append").mockImplementation((...nodes) => {
			script = nodes[0] as HTMLScriptElement;
			receiverName = receiverNameFrom(script);
			append(...nodes);
			queueMicrotask(() => script?.dispatchEvent(new Event("error", { cancelable: true })));
		});

		await expect(loadModule(`throw new Error("broken"); export default {};`)).rejects.toThrow(
			"Failed to execute anywidget module",
		);
		expect(script?.isConnected).toBe(false);
		expect(receivers[receiverName as string]).toBeUndefined();
	});

	test("aborts a pending module and removes its script", async () => {
		const controller = new AbortController();
		let script: HTMLScriptElement | undefined;
		let receiverName: string | undefined;
		const append = document.head.append.bind(document.head);
		vi.spyOn(document.head, "append").mockImplementation((...nodes) => {
			script = nodes[0] as HTMLScriptElement;
			receiverName = receiverNameFrom(script);
			append(...nodes);
		});
		const loading = loadModule(
			`await new Promise(() => {}); export default {};`,
			controller.signal,
		);

		controller.abort(new Error("cancelled"));

		await expect(loading).rejects.toThrow("cancelled");
		expect(script?.isConnected).toBe(false);
		expect(receivers[receiverName as string]).toBeUndefined();
	});
});
