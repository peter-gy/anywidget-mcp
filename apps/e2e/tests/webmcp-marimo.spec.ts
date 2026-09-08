import { expect } from "@playwright/test";
import { executeTool, registeredTools, test } from "./native-webmcp";

test("marimo WebMCP cleans up failed factories and accepts the next creation", async ({ page }) => {
	await page.goto("http://127.0.0.1:8792");
	await expect(page.getByText("WebMCP ready", { exact: true })).toBeVisible();
	const initial = await registeredTools(page);
	const create = initial.find((tool) => tool.name.endsWith("_create_counter"));
	if (!create) throw new Error("The notebook has no creation tool");

	await expect(executeTool(page, create.name, { start: 5, fail: true })).rejects.toThrow();
	expect(await registeredTools(page)).toEqual(initial);
	await expect(page.getByLabel("Counter state")).toHaveCount(0);
	const created = await executeTool(page, create.name, { start: 6 });
	if (!created.tools) throw new Error("The counter has no read tool");
	expect(await executeTool(page, created.tools.read)).toEqual({
		state: { value: 6, doubled: 12 },
	});
	await expect(page.getByLabel("Counter state")).toHaveText("6 / 12");

	await page.getByRole("button", { name: "Restart WebMCP", exact: true }).click();
	await expect(page.getByText("Closed widgets: 2", { exact: true })).toBeVisible();
	await expect(page.getByLabel("Counter state")).toHaveCount(0);
});

test("marimo WebMCP preserves state after validator rejection and accepts the next update", async ({
	page,
}) => {
	await page.goto("http://127.0.0.1:8792");
	await expect(page.getByText("WebMCP ready", { exact: true })).toBeVisible();
	const create = (await registeredTools(page)).find((tool) =>
		tool.name.endsWith("_create_counter"),
	);
	if (!create) throw new Error("The notebook has no creation tool");
	const created = await executeTool(page, create.name, { start: 6 });
	if (!created.tools?.update) throw new Error("The counter has no update tool");
	await expect(executeTool(page, created.tools.update, { value: 13 })).rejects.toThrow();
	expect(await executeTool(page, created.tools.read)).toEqual({
		state: { value: 6, doubled: 12 },
	});
	await expect(page.getByLabel("Counter state")).toHaveText("6 / 12");
	expect(await executeTool(page, created.tools.update, { value: 8 })).toEqual({
		state: { value: 8, doubled: 16 },
	});
	await expect(page.getByLabel("Counter state")).toHaveText("8 / 16");
});

test("marimo WebMCP creates widgets and releases them when the owning cell reruns", async ({
	page,
}) => {
	await page.goto("http://127.0.0.1:8792");
	await expect(page.getByText("WebMCP ready", { exact: true })).toBeVisible();
	await expect(page.getByLabel("Counter state")).toHaveCount(0);
	await expect(page.getByText("Closed widgets: 0", { exact: true })).toBeVisible();
	const initial = await registeredTools(page);
	const names = Object.fromEntries(
		initial.map((tool) => [tool.name.split("_").slice(2).join("_"), tool.name]),
	);
	expect(Object.keys(names).sort()).toEqual(["counter", "create_async_counter", "create_counter"]);
	if (!names.counter || !names.create_counter || !names.create_async_counter)
		throw new Error("Creation tools are unavailable");

	const fromClass = await executeTool(page, names.counter, {
		start: 2,
	});
	const fromFunction = await executeTool(page, names.create_counter, { start: 4 });
	const fromAsync = await executeTool(page, names.create_async_counter, { start: 6 });
	expect(fromClass.state).toEqual({ value: 2, doubled: 4 });
	expect(fromFunction.state).toEqual({ value: 4, doubled: 8 });
	expect(fromAsync.state).toEqual({ value: 6, doubled: 12 });
	expect(fromAsync.tools).toEqual({ read: expect.any(String) });
	expect(new Set([fromClass.widget_id, fromFunction.widget_id, fromAsync.widget_id]).size).toBe(3);
	await expect(page.getByLabel("Counter state")).toHaveText(["2 / 4", "4 / 8", "6 / 12"]);
	if (!fromFunction.tools?.update || !fromAsync.tools)
		throw new Error("The synchronous counter has no update tool");
	expect(await executeTool(page, fromFunction.tools.update, { value: 9 })).toEqual({
		state: { value: 9, doubled: 18 },
	});
	await expect(executeTool(page, fromFunction.tools.update, { notes: "public" })).rejects.toThrow();
	await expect(page.getByLabel("Counter state")).toHaveText(["2 / 4", "9 / 18", "6 / 12"]);
	await page.getByRole("button", { name: "Increment", exact: true }).nth(2).click();
	await expect(page.getByLabel("Counter state")).toHaveText(["2 / 4", "9 / 18", "7 / 14"]);
	expect(await executeTool(page, fromAsync.tools.read, {})).toEqual({
		state: { value: 7, doubled: 14 },
	});

	await page.getByRole("button", { name: "Restart WebMCP", exact: true }).click();
	await expect(page.getByText("Closed widgets: 3", { exact: true })).toBeVisible();
	await expect(page.getByLabel("Counter state")).toHaveCount(0);
	await expect.poll(async () => (await registeredTools(page)).length).toBe(3);
	const restarted = await registeredTools(page);
	expect(restarted.every((tool) => !initial.some((previous) => previous.name === tool.name))).toBe(
		true,
	);
	const counter = restarted.find((tool) => tool.name.split("_").slice(2).join("_") === "counter");
	if (!counter) throw new Error("The restarted session has no counter tool");
	const replacement = await executeTool(page, counter.name, {
		start: 10,
	});
	expect(replacement.widget_id).not.toBe(fromClass.widget_id);
	await expect(page.getByLabel("Counter state")).toHaveText("10 / 20");
});

test("marimo WebMCP keeps notebook clients independent", async ({ page: first, browser }) => {
	await first.goto("http://127.0.0.1:8792");
	await expect(first.getByText("WebMCP ready", { exact: true })).toBeVisible();
	const firstTools = await registeredTools(first);
	const firstCounter = firstTools.find(
		(tool) => tool.name.split("_").slice(2).join("_") === "counter",
	);
	if (!firstCounter) throw new Error("The first notebook has no counter tool");
	const firstWidget = await executeTool(first, firstCounter.name, { start: 2 });

	const second = await browser.newPage();
	try {
		await second.goto("http://127.0.0.1:8792");
		await expect(second.getByText("WebMCP ready", { exact: true })).toBeVisible();
		await expect(second.getByLabel("Counter state")).toHaveCount(0);
		const secondTools = await registeredTools(second);
		expect(
			secondTools.every((tool) => !firstTools.some((previous) => previous.name === tool.name)),
		).toBe(true);
		const secondCounter = secondTools.find(
			(tool) => tool.name.split("_").slice(2).join("_") === "counter",
		);
		if (!secondCounter) throw new Error("The second notebook has no counter tool");
		await executeTool(second, secondCounter.name, { start: 10 });
		await expect(second.getByLabel("Counter state")).toHaveText("10 / 20");
		await expect(first.getByLabel("Counter state")).toHaveText("2 / 4");

		await second.getByRole("button", { name: "Restart WebMCP", exact: true }).click();
		await expect(second.getByText("Closed widgets: 1", { exact: true })).toBeVisible();
		await expect(second.getByLabel("Counter state")).toHaveCount(0);
		if (!firstWidget.tools) throw new Error("The first counter has no read tool");
		expect(await executeTool(first, firstWidget.tools.read)).toEqual({
			state: { value: 2, doubled: 4 },
		});
		await expect(first.getByLabel("Counter state")).toHaveText("2 / 4");
	} finally {
		await second.context().close();
	}
});
