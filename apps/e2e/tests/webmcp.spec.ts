import { expect, projectedState, test } from "./fixtures";

interface CounterState {
	value: number;
	doubled: number;
}

interface Tool {
	name: string;
	execute(
		input: { value?: number },
		options: { signal: AbortSignal },
	): Promise<{ state: CounterState }>;
}

interface ToolDocument extends Document {
	modelContext: {
		tools: Map<string, Tool>;
	};
}

test("WebMCP edits compose with MCP App state delivery and widget interaction", async ({
	page,
}) => {
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
