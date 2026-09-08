import { expect } from "@playwright/test";
import { executeTool, registeredTools, test } from "./native-webmcp";

test("native WebMCP creates a class instance and reads Python-validated interaction", async ({
	page,
}) => {
	await page.goto("/?webmcp");
	await expect(page.getByLabel("Host status")).toHaveText("Ready");
	await page.getByLabel("Widget tool").selectOption("webmcp_workspace");
	await page.getByRole("button", { name: "Open widget", exact: true }).click();
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.getByRole("status")).toHaveText("WebMCP ready");
	await expect.poll(async () => (await registeredTools(page)).length).toBe(2);
	const tools = await registeredTools(page);
	const create = tools.find(
		(tool) => tool.name.endsWith("_counter") && !tool.name.endsWith("_create_counter"),
	);
	if (!create) throw new Error("The counter class has no creation tool");
	expect(create.description.length).toBeGreaterThan(0);
	expect(JSON.parse(create.inputSchema)).toMatchObject({
		type: "object",
		properties: { start: { type: "integer", default: 1 } },
	});
	await expect(widget.getByLabel("Counter state")).toHaveCount(0);

	const created = await executeTool(page, create.name, { start: 6 });
	expect(created).toMatchObject({
		state: { value: 6, doubled: 12 },
		widget_id: expect.any(String),
		tools: { read: expect.any(String), update: expect.any(String) },
	});
	await expect(widget.getByLabel("Counter state")).toHaveText("6 / 12");
	await expect.poll(async () => (await registeredTools(page)).length).toBe(4);
	if (!created.tools?.update) throw new Error("The created counter has no update tool");
	expect(await executeTool(page, created.tools.update, { value: 8 })).toEqual({
		state: { value: 8, doubled: 16 },
	});
	await expect(widget.getByLabel("Counter state")).toHaveText("8 / 16");
	await widget.getByRole("button", { name: "Increment", exact: true }).click();
	await expect(widget.getByLabel("Counter state")).toHaveText("9 / 18");
	expect(await executeTool(page, created.tools.read)).toEqual({
		state: { value: 9, doubled: 18 },
	});

	await page.getByRole("button", { name: "Close widget", exact: true }).click();
	await expect.poll(async () => (await registeredTools(page)).length).toBe(0);
});
