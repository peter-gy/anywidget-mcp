import { expect, type Page } from "@playwright/test";
import { executeTool, registeredTools, test } from "./native-webmcp";

interface JupyterWindow extends Window {
	jupyterapp: {
		restored: Promise<void>;
		commands: { execute(command: string): Promise<void> };
		shell: {
			currentWidget: {
				context: { path: string; ready: Promise<void> };
				revealed: Promise<void>;
				sessionContext: {
					ready: Promise<void>;
					restartKernel(): Promise<boolean>;
					session?: {
						kernel?: { info: Promise<unknown>; connectionStatus: string; status: string };
					};
				};
				content: { activeCellIndex: number };
			};
		};
	};
}

declare const window: JupyterWindow;

interface NotebookSession {
	id: string;
	path: string;
	kernel: { id: string };
}

const server = "http://127.0.0.1:8793";
const headers = { Authorization: "token anywidget-mcp-e2e" };

async function createNotebook(page: Page): Promise<NotebookSession> {
	const copied = await page.request.post(`${server}/api/contents`, {
		headers,
		data: { copy_from: "webmcp.ipynb" },
	});
	expect(copied.status()).toBe(201);
	const { path }: { path: string } = await copied.json();
	try {
		const opened = await page.request.post(`${server}/api/sessions`, {
			headers,
			data: { path, name: path, type: "notebook", kernel: { name: "python3" } },
		});
		expect(opened.status()).toBe(201);
		return await opened.json();
	} catch (error) {
		await page.request.delete(`${server}/api/contents/${path}`, { headers });
		throw error;
	}
}

async function openNotebook(page: Page, session: NotebookSession) {
	// Lab workspaces persist open documents on the server across browser contexts.
	await page.goto(
		`${server}/lab/workspaces/${session.id}/tree/${session.path}?token=anywidget-mcp-e2e`,
	);
	await page.evaluate(async () => window.jupyterapp.restored);
	await expect
		.poll(() => page.evaluate(() => window.jupyterapp.shell.currentWidget?.context?.path))
		.toBe(session.path);
	await expect(page.locator(".jp-Notebook")).toBeVisible();
	await page.waitForFunction(
		async () => {
			const panel = window.jupyterapp.shell.currentWidget;
			await Promise.all([panel.context.ready, panel.revealed, panel.sessionContext.ready]);
			const kernel = panel.sessionContext.session?.kernel;
			if (!kernel) return false;
			// Session readiness precedes the kernel's WebSocket and IOPub handshake.
			await kernel.info;
			return kernel.connectionStatus === "connected" && kernel.status === "idle";
		},
		undefined,
		{ timeout: 30_000 },
	);
}

async function deleteNotebook(page: Page, session: NotebookSession) {
	const shutdown = await page.request.delete(`${server}/api/sessions/${session.id}`, { headers });
	expect([204, 404]).toContain(shutdown.status());
	const deleted = await page.request.delete(`${server}/api/contents/${session.path}`, { headers });
	expect(deleted.status()).toBe(204);
}

async function runCell(page: Page, index: number) {
	await test.step(
		`Run notebook cell ${index}`,
		async () => {
			await page.evaluate(async (index) => {
				window.jupyterapp.shell.currentWidget.content.activeCellIndex = index;
				await window.jupyterapp.commands.execute("notebook:run-cell");
			}, index);
			await expect(
				page.locator(".jp-CodeCell").nth(index).locator(".jp-OutputArea-error"),
			).toHaveCount(0);
		},
		{ timeout: 15_000 },
	);
}

test("JupyterLab exposes existing and newly created widgets through native WebMCP", async ({
	page,
}, testInfo) => {
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 1440, height: 1000 });
	let session: NotebookSession | undefined;
	try {
		session = await createNotebook(page);
		await openNotebook(page, session);
		await runCell(page, 0);
		await expect(page.getByLabel("Counter state")).toHaveText(["2 / 4", "20 / 40"]);
		expect(await registeredTools(page)).toEqual([]);

		await runCell(page, 1);
		await expect(
			page.getByRole("status", { name: "" }).filter({ hasText: "WebMCP ready" }),
		).toBeVisible();
		await runCell(page, 2);
		await expect(page.getByLabel("Counter state")).toHaveText(["2 / 4", "20 / 40", "3 / 6"]);
		await expect.poll(async () => (await registeredTools(page)).length).toBe(6);
		const tools = await registeredTools(page);
		const existing = tools.find(
			(tool) => tool.name.startsWith("existing_") && tool.name.endsWith("_read"),
		);
		const futureUpdate = tools.find((tool) => tool.name.endsWith("_update"));
		if (!existing || !futureUpdate) throw new Error("Discovered counters are missing their tools");
		expect(await executeTool(page, existing.name)).toEqual({ state: { value: 2, doubled: 4 } });
		expect(await executeTool(page, futureUpdate.name, { value: 9 })).toEqual({
			state: { value: 9, doubled: 18 },
		});
		await expect(executeTool(page, futureUpdate.name, { secret: "exposed" })).rejects.toThrow();
		await expect(executeTool(page, futureUpdate.name, { value: 13 })).rejects.toThrow();
		expect(await executeTool(page, futureUpdate.name.replace(/_update$/, "_read"))).toEqual({
			state: { value: 9, doubled: 18 },
		});
		await expect(page.getByLabel("Counter state")).toHaveText(["2 / 4", "20 / 40", "9 / 18"]);
		await page.getByRole("button", { name: "Increment", exact: true }).first().click();
		await expect(page.getByLabel("Counter state").first()).toHaveText("3 / 6");
		expect(await executeTool(page, existing.name)).toEqual({ state: { value: 3, doubled: 6 } });

		const classTool = tools.find(
			(tool) =>
				tool.name.endsWith("_counter") &&
				!tool.name.endsWith("_create_counter") &&
				!tool.name.endsWith("_async_counter"),
		);
		const syncTool = tools.find((tool) => tool.name.endsWith("_create_counter"));
		const asyncTool = tools.find((tool) => tool.name.endsWith("_async_counter"));
		if (!classTool || !syncTool || !asyncTool) throw new Error("Creation tools are missing");
		const first = await executeTool(page, classTool.name, { start: 5 });
		const second = await executeTool(page, syncTool.name);
		const third = await executeTool(page, asyncTool.name, { start: 7 });
		expect(first.state).toEqual({ value: 5, doubled: 10 });
		expect(second.state).toEqual({ value: 4, doubled: 8 });
		expect(third.state).toEqual({ value: 7, doubled: 14 });
		expect(new Set([first.widget_id, second.widget_id, third.widget_id]).size).toBe(3);
		await expect.poll(async () => (await registeredTools(page)).length).toBe(12);
		const created = page.locator(".jp-CodeCell").nth(1);
		await expect(created.getByLabel("Counter state")).toHaveText(["5 / 10", "4 / 8", "7 / 14"]);
		await created.getByRole("button", { name: "Increment", exact: true }).nth(2).click();
		await expect(created.getByLabel("Counter state").nth(2)).toHaveText("8 / 16");
		if (!third.tools) throw new Error("Created widget has no read tool");
		expect(await executeTool(page, third.tools.read)).toEqual({ state: { value: 8, doubled: 16 } });
		await page.screenshot({ path: testInfo.outputPath("jupyter-webmcp.png"), fullPage: true });

		await runCell(page, 3);
		await expect.poll(async () => (await registeredTools(page)).length).toBe(10);
		expect((await registeredTools(page)).some((tool) => tool.name === existing.name)).toBe(false);
		expect((await registeredTools(page)).some((tool) => tool.name === futureUpdate.name)).toBe(
			false,
		);
		await runCell(page, 4);
		await expect.poll(async () => (await registeredTools(page)).length).toBe(0);
		await expect(page.locator(".jp-CodeCell").nth(4)).toContainText(
			"WebMCP disabled, existing widget open: True",
		);
		await runCell(page, 5);
		await expect(page.locator(".jp-CodeCell").nth(5)).toContainText(
			"WebMCP closed, existing widget open: True",
		);
		await expect(page.locator(".jp-CodeCell").nth(5)).toContainText("Created widgets closed: 3");
		await runCell(page, 6);
		await expect.poll(async () => (await registeredTools(page)).length).toBe(1);
		if (!session) throw new Error("Jupyter did not create a notebook session");
		const kernelId = session.kernel.id;
		const shutdown = await page.request.delete(`${server}/api/sessions/${session.id}`, { headers });
		expect(shutdown.status()).toBe(204);
		await expect.poll(async () => (await registeredTools(page)).length).toBe(0);
		const kernels: { id: string }[] = await (
			await page.request.get(`${server}/api/kernels`, { headers })
		).json();
		expect(kernels.some((kernel) => kernel.id === kernelId)).toBe(false);
	} finally {
		if (session) await deleteNotebook(page, session);
	}
});

test("JupyterLab withdraws stale tools on kernel restart and accepts a fresh session", async ({
	page,
}) => {
	test.setTimeout(120_000);
	let session: NotebookSession | undefined;
	try {
		session = await createNotebook(page);
		await openNotebook(page, session);
		await runCell(page, 0);
		await runCell(page, 1);
		await expect.poll(async () => (await registeredTools(page)).length).toBe(4);
		const initial = await registeredTools(page);
		const create = initial.find((tool) => tool.name.endsWith("_create_counter"));
		if (!create) throw new Error("The notebook has no creation tool");
		const created = await executeTool(page, create.name, { start: 6 });
		if (!created.tools) throw new Error("The counter has no read tool");

		await page.evaluate(async () => {
			await window.jupyterapp.shell.currentWidget.sessionContext.restartKernel();
		});
		await expect.poll(async () => (await registeredTools(page)).length).toBe(0);
		await expect(executeTool(page, created.tools.read)).rejects.toThrow("is unavailable");
		await runCell(page, 0);
		await runCell(page, 1);
		await expect.poll(async () => (await registeredTools(page)).length).toBe(4);
		const restarted = await registeredTools(page);
		expect(restarted.every((tool) => !initial.some((old) => old.name === tool.name))).toBe(true);
		const replacement = restarted.find((tool) => tool.name.endsWith("_create_counter"));
		if (!replacement) throw new Error("The restarted kernel has no creation tool");
		const result = await executeTool(page, replacement.name, { start: 8 });
		expect(result.state).toEqual({ value: 8, doubled: 16 });
		await expect(page.locator(".jp-CodeCell").nth(1).getByLabel("Counter state")).toHaveText(
			"8 / 16",
		);
	} finally {
		if (session) await deleteNotebook(page, session);
	}
});
