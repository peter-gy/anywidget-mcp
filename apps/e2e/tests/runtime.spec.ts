import type { Page } from "@playwright/test";
import { expect, projectedState, restoreWidget, test } from "./fixtures";

async function openWidget(page: Page, name: string): Promise<void> {
	await page.getByLabel("Widget tool").selectOption(name);
	await page.getByRole("button", { name: "Open widget", exact: true }).click();
	await expect(page.getByLabel("Host status")).toHaveText("Connected");
}

test("binary edits and reopening commit Python observer results before model context", async ({
	page,
}) => {
	await openWidget(page, "bridge_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.getByTestId("binary-state")).toHaveText("0,127,255 | size 3");
	await widget.getByRole("button", { name: "Change binary" }).click();
	await expect(widget.getByTestId("binary-state")).toHaveText("9,8,7,6 | size 4");
	await expect.poll(() => projectedState(page, "Model context")).toMatchObject({ size: 4 });
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect
		.poll(() => projectedState(page, "Python state"))
		.toMatchObject({
			size: 4,
			payload: { type: "binary", bytes: 4 },
		});
	await restoreWidget(page);
	await expect(widget.getByTestId("binary-state")).toHaveText("0,127,255 | size 3");
	await widget.getByRole("button", { name: "Change binary" }).click();
	await expect(widget.getByTestId("binary-state")).toHaveText("9,8,7,6 | size 4");
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect.poll(() => projectedState(page, "Python state")).toMatchObject({ size: 4 });
});

test("custom commands return text and binary buffers", async ({ page }) => {
	await openWidget(page, "bridge_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await widget.getByRole("button", { name: "Invoke command" }).click();
	await expect(widget.getByTestId("command-result")).toHaveText("retpada | 3,2,1");
});

test("large binary state recovers a lost response and resets on reopening", async ({ page }) => {
	test.slow();
	await page.goto("/?drop-read");
	await openWidget(page, "large_state_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.getByTestId("large-binary")).toHaveText("8388608 | 0 | 255", {
		timeout: 60_000,
	});
	await widget.getByRole("button", { name: "Upload binary", exact: true }).click();
	await expect(widget.getByTestId("large-binary")).toHaveText("8388608 | 11 | 13");
	await expect
		.poll(() => projectedState(page, "Model context"), { timeout: 60_000 })
		.toMatchObject({ payload_checksum: 58720266 });
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect
		.poll(() => projectedState(page, "Python state"))
		.toMatchObject({ payload_size: 8388608, payload_checksum: 58720266 });
	await expect(page.getByLabel("Lost read responses")).toHaveText("1");
	await restoreWidget(page);
	await expect(widget.getByTestId("large-binary")).toHaveText("8388608 | 0 | 255");
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect
		.poll(() => projectedState(page, "Python state"))
		.toMatchObject({ payload_size: 8388608, payload_checksum: 255 });
});

test("large JSON updates preserve Unicode and reset on reopening", async ({ page }) => {
	test.slow();
	await openWidget(page, "large_state_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.getByTestId("large-json")).toHaveText("0 |", { timeout: 60_000 });
	await widget.getByRole("button", { name: "Upload records", exact: true }).click();
	await expect(widget.getByTestId("large-json")).toHaveText("40000 | row 39999 λ");
	await expect
		.poll(() => projectedState(page, "Model context"), { timeout: 60_000 })
		.toMatchObject({ row_count: 40000 });
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect
		.poll(() => projectedState(page, "Python state"))
		.toMatchObject({ row_count: 40000, last_label: "row 39999 λ" });
	await restoreWidget(page);
	await expect(widget.getByTestId("large-json")).toHaveText("0 |");
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect
		.poll(() => projectedState(page, "Python state"))
		.toMatchObject({ row_count: 0, last_label: "" });
});

test("child replacement renders the newly enrolled model", async ({ page }) => {
	await openWidget(page, "bridge_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.getByTestId("child-value")).toHaveText("7");
	await widget.getByRole("button", { name: "Replace child", exact: true }).click();
	await expect(widget.getByTestId("child-value")).toHaveText("11");
});

test("recursive references share a protocol child across replacement and reopening", async ({
	page,
}) => {
	await openWidget(page, "nested_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.getByTestId("nested-ref-count")).toHaveText("3");
	await expect.poll(() => projectedState(page, "Model context")).toEqual({ values: [3, 7, 3] });
	await widget.getByRole("button", { name: "Increment protocol child" }).click();
	await expect.poll(() => projectedState(page, "Model context")).toEqual({ values: [4, 7, 4] });
	await widget.getByRole("button", { name: "Replace nested children" }).click();
	await expect(widget.getByTestId("protocol-value")).toHaveText("20");
	await expect(widget.getByTestId("child-value")).toHaveText("21");
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect.poll(() => projectedState(page, "Python state")).toEqual({ values: [20, 21, 20] });
	await restoreWidget(page);
	await expect(widget.getByTestId("protocol-value")).toHaveText("3");
	await expect(widget.getByTestId("child-value")).toHaveText("7");
	await widget.getByRole("button", { name: "Increment protocol child" }).click();
	await expect.poll(() => projectedState(page, "Model context")).toEqual({ values: [4, 7, 4] });
});

test("source updates replace live code and reopening restores factory sources", async ({
	page,
}) => {
	await openWidget(page, "hot_reload_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.getByTestId("hot-label")).toHaveText("first generation");
	await expect(widget.getByTestId("hot-label")).toHaveCSS("color", "rgb(220, 38, 38)");
	await widget.getByRole("button", { name: "Change CSS" }).click();
	await expect(widget.getByTestId("hot-label")).toHaveCSS("color", "rgb(37, 99, 235)");
	await widget.getByRole("button", { name: "Change ESM" }).click();
	await expect(widget.getByTestId("hot-label")).toHaveText("second generation");
	await expect(widget.getByTestId("hot-label")).toHaveCSS("color", "rgb(37, 99, 235)");
	await restoreWidget(page);
	await expect(widget.getByTestId("hot-label")).toHaveText("first generation");
	await expect(widget.getByTestId("hot-label")).toHaveCSS("color", "rgb(220, 38, 38)");
});

test("large shared sources render on reopening with unavailable storage", async ({ page }) => {
	await openWidget(page, "large_asset_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.getByTestId("large-asset-status")).toHaveText("ready");
	await expect(widget.getByTestId("large-asset-leaf")).toHaveText([
		"first large source",
		"second large source",
	]);
	const requests = Number(await page.getByLabel("Asset requests").innerText());
	expect(requests).toBeGreaterThan(0);
	// New app documents must keep loading when the browser denies cache access.
	await page.addInitScript(() => {
		Object.defineProperty(globalThis, "caches", {
			value: {
				open: () => Promise.reject(new DOMException("Storage denied", "SecurityError")),
			},
		});
	});
	await restoreWidget(page);
	await expect(widget.getByTestId("large-asset-status")).toHaveText("ready");

	await expect(widget.getByTestId("large-asset-leaf")).toHaveText([
		"first large source",
		"second large source",
	]);
	await expect
		.poll(async () => Number(await page.getByLabel("Asset requests").innerText()))
		.toBeGreaterThan(requests);
});

test("a rejected trait update disposes the session and permits explicit reopening", async ({
	page,
}) => {
	await openWidget(page, "validation_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await widget.getByRole("button", { name: "Send invalid value" }).click();
	await expect(widget.getByText(/expected an int/)).toBeVisible();
	await expect(page.getByLabel("Disposed sessions")).toHaveText("1");
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect(page.getByLabel("Python state")).toContainText('"isError":true');
	await widget.getByRole("button", { name: "Reopen", exact: true }).click();
	await expect(widget.getByRole("button", { name: "Send invalid value" })).toBeVisible();
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect.poll(() => projectedState(page, "Python state")).toMatchObject({ value: 1 });
});

test("failed widget sessions release state and allow subsequent launches", async ({ page }) => {
	const failures = [
		["load", "Widget module loading failed"],
		["initialize", "Widget initialization failed"],
		["render", "Widget rendering failed"],
	] as const;
	for (const [index, [phase, message]] of failures.entries()) {
		// eslint-disable-next-line no-await-in-loop -- Each failure must release its session before the next launch.
		await test.step(phase, async () => {
			await page.getByLabel("Tool arguments").fill(JSON.stringify({ phase }));
			await openWidget(page, "lifecycle_probe");
			const widget = page.frameLocator('iframe[title="Widget"]');
			await expect(widget.getByRole("alert")).toContainText(message);
			await expect(widget.getByRole("alert")).toContainText("new widget");
			await expect(page.getByLabel("Disposed sessions")).toHaveText(String(index + 1));
			await page.getByRole("button", { name: "Read Python state" }).click();
			await expect(page.getByLabel("Python state")).toContainText('"isError":true');
		});
	}

	await page.getByLabel("Tool arguments").fill("{}");
	await openWidget(page, "bridge_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await widget.getByRole("button", { name: "Change binary" }).click();
	await expect(widget.getByTestId("binary-state")).toHaveText("9,8,7,6 | size 4");
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect.poll(() => projectedState(page, "Python state")).toMatchObject({ size: 4 });
});

test("hosts can pull current Python state without context delivery", async ({ page }) => {
	await page.goto("/?pull-only");
	await expect(page.getByLabel("Host status")).toHaveText("Ready");
	await openWidget(page, "bridge_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await widget.getByRole("button", { name: "Change binary" }).click();
	await expect(widget.getByTestId("binary-state")).toHaveText("9,8,7,6 | size 4");
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect.poll(() => projectedState(page, "Python state")).toMatchObject({ size: 4 });
	await expect(page.getByLabel("Model context")).toBeEmpty();
});

test("reopening reruns initialization and creates a fresh startup event backlog", async ({
	page,
}) => {
	test.slow();
	await openWidget(page, "startup_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.getByTestId("startup-progress")).toHaveText("Stage 41, 250 events", {
		timeout: 60_000,
	});
	await expect.poll(() => projectedState(page, "Model context")).toEqual({ stage: 41 });
	await widget.getByRole("button", { name: "Resume live events" }).click();
	await expect(widget.getByTestId("startup-progress")).toHaveText("Stage 41, 251 events");
	await restoreWidget(page);
	await expect(widget.getByTestId("startup-progress")).toHaveText("Stage 41, 250 events");
});

test("deep projections preserve complete values through edits and reopening", async ({ page }) => {
	await openWidget(page, "projection_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.getByRole("status")).toHaveText(
		"200 records; 1500 characters; 300 levels; complete",
	);
	const payload = {
		values: Array.from({ length: 200 }, (_, index) => index),
		text: "λ".repeat(1500),
		nested: JSON.parse("[".repeat(300) + '"complete"' + "]".repeat(300)),
		["k".repeat(500)]: "full key",
	};
	await expect.poll(() => projectedState(page, "Model context")).toEqual({ payload });
	await widget.getByRole("button", { name: "Update deep value" }).click();
	await expect(widget.getByRole("status")).toHaveText(
		"200 records; 1500 characters; 300 levels; updated",
	);
	payload.nested = JSON.parse("[".repeat(300) + '"updated"' + "]".repeat(300));
	await expect.poll(() => projectedState(page, "Model context")).toEqual({ payload });
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect(page.getByLabel("Python state")).toContainText("Current projection_probe state:");
	const result = await page
		.getByLabel("Python state")
		.evaluate((output) => JSON.parse(output.textContent || "{}"));
	expect(result.structuredContent.tool).toBe("projection_probe");
	const text = result.content[0].text;
	expect(JSON.parse(text.slice(text.indexOf("{"), text.lastIndexOf("}") + 1))).toEqual({ payload });
	await restoreWidget(page);
	await expect(widget.getByRole("status")).toHaveText(
		"200 records; 1500 characters; 300 levels; complete",
	);
});
