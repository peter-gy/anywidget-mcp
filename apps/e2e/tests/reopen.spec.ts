import type { Page, Route } from "@playwright/test";
import { z } from "zod";
import { spawn, type ChildProcess } from "node:child_process";
import { once } from "node:events";
import { createServer } from "node:net";
import { resolve } from "node:path";
import { test as base, expect, projectedState } from "./fixtures";

interface ExplorerServer {
	url: string;
	restart(): Promise<void>;
	events(): Promise<Array<{ event: string; resource: string; widget_closed?: boolean }>>;
}

const test = base.extend<{ explorer: ExplorerServer; sessionMode: "stateful" | "stateless" }>({
	sessionMode: ["stateless", { option: true }],
	explorer: async ({ request, sessionMode }, use) => {
		const socket = createServer();
		socket.listen(0, "127.0.0.1");
		await once(socket, "listening");
		const { port } = z.object({ port: z.number() }).parse(socket.address());
		await new Promise<void>((done) => socket.close(() => done()));
		const url = `http://127.0.0.1:${port}`;
		let process: ChildProcess | undefined;
		let output = "";
		const stop = async () => {
			if (!process || process.exitCode !== null) return;
			const exited = once(process, "exit");
			await request.post(`${url}/fault`, { data: { mode: "none" } }).catch(() => undefined);
			const child = process;
			child.kill("SIGTERM");
			const timeout = setTimeout(() => child.kill("SIGKILL"), 5000);
			try {
				await exited;
			} finally {
				clearTimeout(timeout);
			}
		};
		const start = async () => {
			process = spawn(
				"uv",
				[
					"run",
					"--locked",
					"--no-sync",
					"--package",
					"anywidget-mcp",
					"python",
					"apps/e2e/reopen_server.py",
					"--port",
					String(port),
					"--mode",
					sessionMode,
					"--idle-timeout",
					"3",
				],
				{
					cwd: resolve(import.meta.dirname, "../../.."),
					stdio: ["ignore", "pipe", "pipe"],
				},
			);
			process.stdout?.resume();
			process.stderr?.on("data", (data: Buffer) => {
				output += data.toString();
			});
			await expect
				.poll(async () => {
					if (process?.exitCode !== null) throw new Error(output);
					return request.get(`${url}/events`).then(
						(response) => response.ok(),
						() => false,
					);
				})
				.toBe(true);
		};
		try {
			await start();
			await use({
				url,
				restart: async () => {
					await stop();
					await start();
				},
				events: async () => (await fetch(`${url}/events`)).json(),
			});
		} finally {
			await stop();
		}
	},
});

async function openExplorer(page: Page, url: string, mode: string, options = "") {
	await page.goto(`/?server=${encodeURIComponent(`${url}/mcp`)}${options}`);
	await expect(page.getByLabel("Host status")).toHaveText("Ready");
	await page.getByLabel("Widget tool").selectOption(`explore_${mode}`);
	await page.getByLabel("Tool arguments").fill('{"query":{"values":["3",7]}}');
	await page.getByRole("button", { name: "Open widget", exact: true }).click();
	await expect(page.frameLocator("iframe").getByTestId("selected-value")).toHaveText("6");
}

test("saved inputs reopen a fresh SQLite resource after a process restart", async ({
	page,
	explorer,
}) => {
	await openExplorer(page, explorer.url, "manual");
	const frame = page.frameLocator("iframe");
	const original = await frame.getByTestId("resource").textContent();
	await frame.getByTestId("next").click();
	await expect(frame.getByTestId("selected-value")).toHaveText("14");
	await frame.getByTestId("export").click();
	await expect(frame.getByTestId("exports")).toHaveText("1");
	await page.getByRole("button", { name: "Close widget", exact: true }).click();
	await expect
		.poll(async () => (await explorer.events()).at(-1))
		.toMatchObject({ event: "close", widget_closed: true });
	await explorer.restart();
	await page.reload();
	await expect(page.getByLabel("Host status")).toHaveText("Ready");
	await page.getByRole("button", { name: "Restore saved result" }).click();
	await expect(frame.getByRole("button", { name: "Reopen", exact: true })).toBeVisible();
	expect(await explorer.events()).toEqual([]);
	await frame.getByRole("button", { name: "Reopen", exact: true }).click();
	await expect(frame.getByTestId("selected-value")).toHaveText("6");
	await expect(frame.getByTestId("resource")).not.toHaveText(original!);
	await expect(frame.getByTestId("exports")).toHaveText("0");
	await frame.getByTestId("next").click();
	await expect(frame.getByTestId("selected-value")).toHaveText("14");
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect
		.poll(() => projectedState(page, "Python state"))
		.toMatchObject({ selected_value: 14 });
	expect((await explorer.events()).map((event) => event.event)).toEqual(["open"]);
});

test("a lost recreation response is not retried automatically", async ({ page, explorer }) => {
	await openExplorer(page, explorer.url, "auto", "&drop-reopen");
	await page.getByRole("button", { name: "Restore saved result" }).click();
	const frame = page.frameLocator("iframe");
	await expect(frame.getByRole("alert")).toContainText("Creation response lost");
	await expect(frame.getByRole("alert")).toHaveAttribute("data-error-code", "reopen_unconfirmed");
	await expect(frame.getByRole("alert")).toContainText("may have completed");
	await expect(frame.getByRole("button", { name: "Reopen", exact: true })).toBeVisible();
	expect((await explorer.events()).filter((event) => event.event === "open")).toHaveLength(2);
	await frame.getByRole("button", { name: "Reopen", exact: true }).click();
	await expect(frame.getByTestId("selected-value")).toHaveText("6");
	expect((await explorer.events()).filter((event) => event.event === "open")).toHaveLength(3);
});

test("independent views recreate independent graphs while the original remains live", async ({
	page,
	explorer,
	context,
}) => {
	await openExplorer(page, explorer.url, "auto");
	const original = page.frameLocator("iframe");
	await original.getByTestId("next").click();
	await expect(original.getByTestId("selected-value")).toHaveText("14");
	const saved = await page.evaluate(() => sessionStorage.getItem("saved-result"));
	const second = await context.newPage();
	try {
		await second.goto(`/?server=${encodeURIComponent(`${explorer.url}/mcp`)}`);
		await expect(second.getByLabel("Host status")).toHaveText("Ready");
		await second.evaluate((result) => {
			sessionStorage.setItem("saved-result", result!);
			sessionStorage.setItem("saved-tool", "explore_auto");
		}, saved);
		await second.reload();
		await expect(second.getByLabel("Host status")).toHaveText("Ready");
		await second.getByRole("button", { name: "Restore saved result" }).click();
		await expect(second.frameLocator("iframe").getByTestId("selected-value")).toHaveText("6");
		await expect(original.getByTestId("selected-value")).toHaveText("14");
		expect((await explorer.events()).map((event) => event.event)).toEqual(["open", "open"]);
		await second.getByRole("button", { name: "Close widget", exact: true }).click();
		await original.getByTestId("next").click();
		await expect(original.getByTestId("selected-value")).toHaveText("6");
	} finally {
		await second.close();
	}
});

test("an active session lost on restart requires an explicit reopen even in auto mode", async ({
	page,
	explorer,
}) => {
	await openExplorer(page, explorer.url, "auto");
	let release!: () => void;
	let intercepted = false;
	const barrier = new Promise<void>((done) => {
		release = done;
	});
	await interceptTool(page, explorer.url, "anywidget_poll", async (route) => {
		intercepted = true;
		await barrier;
		await route.continue();
	});
	try {
		await expect.poll(() => intercepted).toBe(true);
		await explorer.restart();
	} finally {
		release();
	}
	const frame = page.frameLocator("iframe");
	await expect(frame.getByRole("button", { name: "Reopen", exact: true })).toBeVisible();
	await expect(frame.getByTestId("next")).not.toBeVisible();
	expect(await explorer.events()).toEqual([]);
	await frame.getByRole("button", { name: "Reopen", exact: true }).click();
	await expect(frame.getByTestId("selected-value")).toHaveText("6");
	expect((await explorer.events()).map((event) => event.event)).toEqual(["open"]);
});

async function interceptTool(
	page: Page,
	url: string,
	name: string,
	handle: (route: Route) => Promise<void>,
) {
	await page.route(`${url}/mcp`, async (route) => {
		if (
			route.request().method() === "POST" &&
			route.request().postDataJSON()?.params?.name === name
		)
			await handle(route);
		else await route.continue();
	});
}

for (const fault of ["reject", "crash", "render"] as const) {
	test(`reopening handles ${fault} failures with an actionable message and a fresh retry`, async ({
		page,
		explorer,
		request,
	}) => {
		await openExplorer(page, explorer.url, "auto");
		await request.post(`${explorer.url}/fault`, { data: { mode: fault } });
		await page.getByRole("button", { name: "Restore saved result" }).click();
		const frame = page.frameLocator("iframe");
		await expect(frame.getByRole("alert")).toContainText("explore_auto");
		await expect(frame.getByRole("alert")).toHaveAttribute(
			"data-error-code",
			fault === "render" ? "reopen_mount_failed" : "reopen_rejected",
		);
		if (fault === "crash")
			await expect(frame.getByRole("alert")).not.toContainText("internal-credential-must-not-leak");
		await expect(frame.getByRole("alert")).toContainText("Reopen");
		await expect(frame.getByRole("button", { name: "Reopen", exact: true })).toBeVisible();
		if (fault === "reject")
			await expect(frame.getByRole("alert")).toContainText("Choose another dataset");
		await request.post(`${explorer.url}/fault`, { data: { mode: "none" } });
		await frame.getByRole("button", { name: "Reopen", exact: true }).click();
		await expect(frame.getByTestId("selected-value")).toHaveText("6");
		const events = await explorer.events();
		const current = await frame.getByTestId("resource").textContent();
		const replaced = events.filter((event) => event.event === "open" && event.resource !== current);
		expect(events.filter((event) => event.event === "close")).toEqual(
			replaced.map(({ resource }) => ({ event: "close", resource, widget_closed: true })),
		);
	});
}

test("saved inputs rejected by current validation do not reach the factory", async ({
	page,
	explorer,
}) => {
	await openExplorer(page, explorer.url, "auto");
	await page.evaluate(() => {
		const saved = JSON.parse(sessionStorage.getItem("saved-result")!);
		saved._meta.anywidget.reopen.arguments.query.values = [];
		sessionStorage.setItem("saved-result", JSON.stringify(saved));
	});
	await page.getByRole("button", { name: "Restore saved result" }).click();
	const frame = page.frameLocator("iframe");
	await expect(frame.getByRole("alert")).toContainText("query.values");
	await expect(frame.getByRole("alert")).toContainText("new widget");
	expect((await explorer.events()).map((event) => event.event)).toEqual(["open", "close"]);
});

test("a broken bootstrap never triggers automatic factory execution", async ({
	page,
	explorer,
}) => {
	await openExplorer(page, explorer.url, "auto");
	await interceptTool(page, explorer.url, "anywidget_bootstrap", async (route) => {
		await route.fulfill({
			json: {
				jsonrpc: "2.0",
				id: route.request().postDataJSON().id,
				result: { content: [], _meta: { anywidget: { protocolVersion: 99 } } },
			},
		});
	});
	await page.getByRole("button", { name: "Restore saved result" }).click();
	await expect(page.frameLocator("iframe").getByRole("alert")).toContainText("Widget");
	expect((await explorer.events()).filter((event) => event.event === "open")).toHaveLength(1);
});

test("missing saved metadata explains why reopening cannot run", async ({ page, explorer }) => {
	await openExplorer(page, explorer.url, "auto");
	await page.evaluate(() => {
		const saved = JSON.parse(sessionStorage.getItem("saved-result")!);
		delete saved._meta.anywidget;
		sessionStorage.setItem("saved-result", JSON.stringify(saved));
	});
	await page.getByRole("button", { name: "Restore saved result" }).click();
	const frame = page.frameLocator("iframe");
	await expect(frame.getByRole("alert")).toContainText("new widget");
	await expect(frame.getByRole("button", { name: "Reopen", exact: true })).toHaveCount(0);
	expect((await explorer.events()).filter((event) => event.event === "open")).toHaveLength(1);
});

test("successive automatic reopens deduplicate results, reset actions, and close each resource", async ({
	page,
	explorer,
}) => {
	await openExplorer(page, explorer.url, "auto", "&echo-result");
	const frame = page.frameLocator("iframe");
	const identities = new Set([await frame.getByTestId("resource").textContent()]);
	/* eslint-disable no-await-in-loop -- Each replacement must finish before exercising the next session. */
	for (let cycle = 0; cycle < 2; cycle++) {
		await frame.getByTestId("next").click();
		await expect(frame.getByTestId("selected-value")).toHaveText("14");
		await frame.getByTestId("export").click();
		await expect(frame.getByTestId("exports")).toHaveText("1");
		await page.getByRole("button", { name: "Restore saved result" }).click();
		await expect(frame.getByTestId("selected-value")).toHaveText("6");
		await expect(frame.getByTestId("exports")).toHaveText("0");
		identities.add(await frame.getByTestId("resource").textContent());
	}
	/* eslint-enable no-await-in-loop */
	expect(identities.size).toBe(3);
	await page.getByRole("button", { name: "Close widget", exact: true }).click();
	await expect(page.getByLabel("Host status")).toHaveText("Closed");
	const events = await explorer.events();
	const opened = events.filter((event) => event.event === "open").map((event) => event.resource);
	expect(opened).toHaveLength(3);
	expect(new Set(opened)).toEqual(identities);
	expect(events.filter((event) => event.event === "close")).toEqual(
		opened.map((resource) => ({ event: "close", resource, widget_closed: true })),
	);
	expect(events.filter((event) => event.event === "export")).toHaveLength(2);
});

test.describe("stateful transport", () => {
	test.use({ sessionMode: "stateful" });
	test("rapid reopen clicks share one pending acquisition and cancellation releases it", async ({
		page,
		explorer,
		request,
	}) => {
		await openExplorer(page, explorer.url, "manual");
		await page.getByRole("button", { name: "Restore saved result" }).click();
		const frame = page.frameLocator("iframe");
		const reopen = frame.getByRole("button", { name: "Reopen", exact: true });
		await expect(reopen).toBeVisible();
		await request.post(`${explorer.url}/fault`, { data: { mode: "wait" } });
		await reopen.evaluate((button) => {
			if (!(button instanceof HTMLButtonElement)) throw new Error("Expected a Reopen button");
			for (let click = 0; click < 20; click++) button.click();
		});
		await expect
			.poll(async () => (await explorer.events()).filter((event) => event.event === "open").length)
			.toBe(2);
		await expect(reopen).not.toBeVisible();
		await page.getByRole("button", { name: "Close widget", exact: true }).click();
		await expect(page.getByLabel("Host status")).toHaveText("Closed");
		await expect
			.poll(async () =>
				(await explorer.events())
					.filter((event) => event.event === "close")
					.map((event) => event.widget_closed),
			)
			.toEqual([true, true]);
		await request.post(`${explorer.url}/fault`, { data: { mode: "none" } });
		await expect(page.locator("iframe")).toHaveCount(0);
		expect((await explorer.events()).map((event) => event.event)).toEqual([
			"open",
			"close",
			"open",
			"close",
		]);
	});
});

test("repeatedly unavailable new bootstraps stop after one automatic creation", async ({
	page,
	explorer,
}) => {
	await openExplorer(page, explorer.url, "auto");
	await interceptTool(page, explorer.url, "anywidget_bootstrap", async (route) => {
		await route.fulfill({
			json: {
				jsonrpc: "2.0",
				id: route.request().postDataJSON().id,
				result: {
					content: [{ type: "text", text: "Session expired during initialization" }],
					isError: true,
					_meta: { anywidget: { error: "session_unavailable" } },
				},
			},
		});
	});
	await page.getByRole("button", { name: "Restore saved result" }).click();
	const frame = page.frameLocator("iframe");
	await expect(frame.getByRole("button", { name: "Reopen", exact: true })).toBeVisible();
	await expect
		.poll(async () => (await explorer.events()).filter((event) => event.event === "close").length)
		.toBe(2);
	expect((await explorer.events()).filter((event) => event.event === "open")).toHaveLength(2);
});

test("a terminal transport failure stops the old graph and offers explicit recovery", async ({
	page,
	explorer,
}) => {
	await openExplorer(page, explorer.url, "auto");
	await interceptTool(page, explorer.url, "anywidget_poll", (route) =>
		route.abort("connectionfailed"),
	);
	const frame = page.frameLocator("iframe");
	await expect(frame.getByRole("alert")).toContainText("stopped");
	await expect(frame.getByRole("alert")).toHaveAttribute("data-error-code", "runtime_stopped");
	await expect(frame.getByRole("button", { name: "Reopen", exact: true })).toBeVisible();
	await expect(frame.getByTestId("next")).not.toBeVisible();
	expect((await explorer.events()).filter((event) => event.event === "open")).toHaveLength(1);
	await page.unroute(`${explorer.url}/mcp`);
	await frame.getByRole("button", { name: "Reopen", exact: true }).click();
	await expect(frame.getByTestId("selected-value")).toHaveText("6");
});

test("stateless cancellation ignores late results and idle expiry releases unclaimed resources", async ({
	page,
	explorer,
	request,
}) => {
	await openExplorer(page, explorer.url, "auto");
	await request.post(`${explorer.url}/fault`, { data: { mode: "wait" } });
	await page.getByRole("button", { name: "Restore saved result" }).click();
	await expect
		.poll(async () => (await explorer.events()).filter((event) => event.event === "open").length)
		.toBe(2);
	await page.getByRole("button", { name: "Close widget", exact: true }).click();
	await expect(page.getByLabel("Host status")).toHaveText("Closed");
	await request.post(`${explorer.url}/fault`, { data: { mode: "none" } });
	await expect
		.poll(async () => (await explorer.events()).filter((event) => event.event === "close").length)
		.toBe(2);
	await expect(page.locator("iframe")).toHaveCount(0);
	expect((await explorer.events()).filter((event) => event.event === "open")).toHaveLength(2);
});

test("a late response from a closed app cannot replace a newer widget or its state handle", async ({
	page,
	explorer,
}) => {
	await openExplorer(page, explorer.url, "auto", "&echo-result");
	let release!: () => void;
	let ready = false;
	let delivered = false;
	let holding = false;
	const barrier = new Promise<void>((done) => {
		release = done;
	});
	await interceptTool(page, explorer.url, "explore_auto", async (route) => {
		if (holding) {
			await route.continue();
			return;
		}
		holding = true;
		const response = await route.fetch();
		ready = true;
		await barrier;
		try {
			await route.fulfill({ response });
		} finally {
			delivered = true;
		}
	});
	const frame = page.frameLocator("iframe");
	let current: string | null;
	try {
		await page.getByRole("button", { name: "Restore saved result" }).click();
		await expect.poll(() => ready).toBe(true);
		await page.getByLabel("Tool arguments").fill('{"query":{"values":[13]}}');
		await page.getByRole("button", { name: "Open widget", exact: true }).click();
		await expect(frame.getByTestId("selected-value")).toHaveText("26");
		current = await frame.getByTestId("resource").textContent();
	} finally {
		release();
	}
	await expect.poll(() => delivered).toBe(true);
	await expect
		.poll(async () => (await explorer.events()).filter((event) => event.event === "close").length)
		.toBe(2);
	await expect(frame.getByTestId("resource")).toHaveText(current!);
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect
		.poll(() => projectedState(page, "Python state"))
		.toMatchObject({ selected_value: 26 });
	expect((await explorer.events()).filter((event) => event.event === "open")).toHaveLength(3);
});

test("host refusal identifies the attempted tool and does not execute or retry the factory", async ({
	page,
	explorer,
}) => {
	await openExplorer(page, explorer.url, "auto");
	await interceptTool(page, explorer.url, "explore_auto", (route) =>
		route.fulfill({ status: 403, body: "Tool access denied" }),
	);
	await page.getByRole("button", { name: "Restore saved result" }).click();
	const frame = page.frameLocator("iframe");
	await expect(frame.getByRole("alert")).toContainText("explore_auto");
	await expect(frame.getByRole("alert")).toContainText("403");
	await expect(frame.getByRole("alert")).toHaveAttribute("data-error-code", "reopen_unconfirmed");
	expect((await explorer.events()).filter((event) => event.event === "open")).toHaveLength(1);
});

test("automatic recovery works without custom controls, including after a failed attempt", async ({
	page,
	explorer,
	request,
}) => {
	await openExplorer(page, explorer.url, "quiet");
	const frame = page.frameLocator("iframe");
	const original = await frame.getByTestId("resource").textContent();
	await expect(frame.locator("#reopen")).toHaveCount(0);
	await frame.getByTestId("next").click();
	await expect(frame.getByTestId("selected-value")).toHaveText("14");
	await page.getByRole("button", { name: "Restore saved result" }).click();
	await expect(frame.getByTestId("selected-value")).toHaveText("6");
	await expect(frame.getByTestId("resource")).not.toHaveText(original!);
	await expect(frame.locator("#reopen")).toHaveCount(0);
	await request.post(`${explorer.url}/fault`, { data: { mode: "reject" } });
	await page.getByRole("button", { name: "Restore saved result" }).click();
	await expect(frame.getByRole("alert")).toHaveAttribute("data-error-code", "reopen_rejected");
	await expect(frame.getByRole("alert")).toContainText("Reload this result");
	await expect(frame.locator("#reopen")).toHaveCount(0);
	await request.post(`${explorer.url}/fault`, { data: { mode: "none" } });
	await page.getByRole("button", { name: "Restore saved result" }).click();
	await expect(frame.getByTestId("selected-value")).toHaveText("6");
	await expect(frame.locator("#reopen")).toHaveCount(0);
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect
		.poll(() => projectedState(page, "Python state"))
		.toMatchObject({ selected_value: 6 });
	expect((await explorer.events()).filter((event) => event.event === "open")).toHaveLength(3);
});
