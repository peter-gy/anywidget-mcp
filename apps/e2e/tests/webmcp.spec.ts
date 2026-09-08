import type { Page } from "@playwright/test";
import { expect, projectedState, test } from "./fixtures";

interface CounterState {
	value: number;
	doubled: number;
}

interface ToolInput {
	start?: number | string;
	locked?: boolean;
	value?: number;
	secret?: string;
}

interface Tool {
	name: string;
	execute(
		input: ToolInput,
		options: { signal: AbortSignal },
	): Promise<{
		state: CounterState;
		widget_id?: string;
		ref?: string;
		tools?: { read: string; update?: string };
	}>;
}

interface ToolDocument extends Document {
	modelContext: {
		tools: Map<string, Tool>;
	};
}

test.beforeEach(async ({ page }) => {
	// The registry controls the browser API boundary across the browser matrix.
	// Calls still cross the packaged MCP App, MCP server, and Python trait observers.
	await page.addInitScript(() => {
		const tools = new Map<string, Tool>();
		Object.defineProperty(document, "modelContext", {
			configurable: true,
			value: {
				tools,
				registerTool(tool: Tool, { signal }: { signal: AbortSignal }) {
					signal.throwIfAborted();
					if (tools.has(tool.name)) return Promise.reject(new Error("Duplicate WebMCP tool"));
					tools.set(tool.name, tool);
					signal.addEventListener("abort", () => tools.delete(tool.name), { once: true });
					return Promise.resolve();
				},
			},
		});
	});
	await page.goto("/?webmcp");
	await expect(page.getByLabel("Host status")).toHaveText("Ready");
});

test("WebMCP edits compose with MCP App state delivery and widget interaction", async ({
	page,
}) => {
	await page.getByLabel("Widget tool").selectOption("webmcp_counter");
	await page.getByRole("button", { name: "Open widget", exact: true }).click();
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.getByLabel("Counter state")).toHaveText("1 / 2");
	await expect
		.poll(() =>
			page.evaluate(() => {
				const context: ToolDocument["modelContext"] = Object.getOwnPropertyDescriptor(
					document,
					"modelContext",
				)?.value;
				return context.tools.size;
			}),
		)
		.toBe(2);

	const result = await page.evaluate(async () => {
		const context: ToolDocument["modelContext"] = Object.getOwnPropertyDescriptor(
			document,
			"modelContext",
		)?.value;
		const tools = context.tools;
		const update = [...tools.values()].find((tool) => tool.name.endsWith("_update"));
		if (!update) throw new Error("The rendered counter has no update tool");
		return update.execute({ value: 8 }, { signal: new AbortController().signal });
	});
	expect(result).toEqual({ state: { value: 8, doubled: 16 } });
	await expect(widget.getByLabel("Counter state")).toHaveText("8 / 16");
	await expect.poll(() => projectedState(page, "Model context")).toEqual({ value: 8, doubled: 16 });
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect.poll(() => projectedState(page, "Python state")).toEqual({ value: 8, doubled: 16 });

	await widget.getByRole("button", { name: "Increment", exact: true }).click();
	await expect(widget.getByLabel("Counter state")).toHaveText("9 / 18");
	const read = await page.evaluate(async () => {
		const context: ToolDocument["modelContext"] = Object.getOwnPropertyDescriptor(
			document,
			"modelContext",
		)?.value;
		const tools = context.tools;
		const read = [...tools.values()].find((tool) => tool.name.endsWith("_read"));
		if (!read) throw new Error("The rendered counter has no read tool");
		return read.execute({}, { signal: new AbortController().signal });
	});
	expect(read).toEqual({ state: { value: 9, doubled: 18 } });

	await page.getByRole("button", { name: "Close widget", exact: true }).click();
	await expect
		.poll(() =>
			page.evaluate(() => {
				const context: ToolDocument["modelContext"] = Object.getOwnPropertyDescriptor(
					document,
					"modelContext",
				)?.value;
				return context.tools.size;
			}),
		)
		.toBe(0);
});

async function executeTool(page: Page, name: string, input: ToolInput = {}) {
	return page.evaluate(
		async ({ name, input }) => {
			const context: ToolDocument["modelContext"] = Object.getOwnPropertyDescriptor(
				document,
				"modelContext",
			)?.value;
			const tool = [...context.tools.values()].find(
				(candidate) =>
					candidate.name === name || candidate.name.split("_").slice(2).join("_") === name,
			);
			if (!tool) throw new Error(`WebMCP tool ${name} is unavailable`);
			return tool.execute(input, { signal: new AbortController().signal });
		},
		{ name, input },
	);
}

test("WebMCP creates independent widgets with typed inputs and instance policies", async ({
	page,
}) => {
	await page.getByLabel("Widget tool").selectOption("webmcp_workspace");
	await page.getByRole("button", { name: "Open widget", exact: true }).click();
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect
		.poll(() => projectedState(page, "Model context"))
		.toEqual({
			created: 0,
			counters: [],
		});
	await expect
		.poll(() =>
			page.evaluate(() => {
				const context: ToolDocument["modelContext"] = Object.getOwnPropertyDescriptor(
					document,
					"modelContext",
				)?.value;
				return [...context.tools.values()]
					.map((tool) => tool.name.split("_").slice(2).join("_"))
					.sort();
			}),
		)
		.toEqual(["counter", "create_counter"]);
	await expect(widget.getByLabel("Counter state")).toHaveCount(0);

	await expect(executeTool(page, "create_counter", { start: "invalid" })).rejects.toThrow();
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect
		.poll(() => projectedState(page, "Python state"))
		.toEqual({ created: 0, counters: [] });

	const first = await executeTool(page, "counter");
	expect(first).toMatchObject({
		state: { value: 1, doubled: 2 },
		widget_id: expect.any(String),
		ref: expect.any(String),
		tools: { read: expect.any(String), update: expect.any(String) },
	});
	await expect(widget.getByLabel("Counter state")).toHaveText(["1 / 2"]);

	const second = await executeTool(page, "create_counter", { locked: true });
	expect(second.state).toEqual({ value: 4, doubled: 8 });
	expect(second.tools).toEqual({ read: expect.any(String) });
	expect(second.widget_id).not.toBe(first.widget_id);
	await expect(widget.getByLabel("Counter state")).toHaveText(["1 / 2", "4 / 8"]);

	const updateName = first.tools?.update ?? "missing_update";
	expect(await executeTool(page, updateName, { value: 8 })).toEqual({
		state: { value: 8, doubled: 16 },
	});
	await expect(executeTool(page, updateName, { secret: "exposed" })).rejects.toThrow();
	await expect(widget.getByLabel("Counter state")).toHaveText(["8 / 16", "4 / 8"]);

	await widget.getByRole("button", { name: "Increment", exact: true }).nth(1).click();
	await expect(widget.getByLabel("Counter state")).toHaveText(["8 / 16", "5 / 10"]);
	expect(await executeTool(page, second.tools?.read ?? "missing_read")).toEqual({
		state: { value: 5, doubled: 10 },
	});
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect
		.poll(() => projectedState(page, "Python state"))
		.toEqual({
			created: 2,
			counters: [
				{ value: 8, doubled: 16 },
				{ value: 5, doubled: 10 },
			],
		});
	await expect
		.poll(() => projectedState(page, "Model context"))
		.toEqual({
			created: 2,
			counters: [
				{ value: 8, doubled: 16 },
				{ value: 5, doubled: 10 },
			],
		});

	await page.getByRole("button", { name: "Close widget", exact: true }).click();
	await expect
		.poll(() =>
			page.evaluate(() => {
				const context: ToolDocument["modelContext"] = Object.getOwnPropertyDescriptor(
					document,
					"modelContext",
				)?.value;
				return context.tools.size;
			}),
		)
		.toBe(0);
});
