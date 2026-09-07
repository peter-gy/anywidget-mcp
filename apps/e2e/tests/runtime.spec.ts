import type { Page } from "@playwright/test";
import { expect, projectedState, test } from "./fixtures";

async function openWidget(page: Page, name: string): Promise<void> {
	await page.getByLabel("Widget tool").selectOption(name);
	await page.getByRole("button", { name: "Open widget", exact: true }).click();
	await expect(page.getByLabel("Host status")).toHaveText("Connected");
}

test("binary edits commit Python observer results before model context", async ({ page }) => {
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
});

test("custom commands return text and binary buffers", async ({ page }) => {
	await openWidget(page, "bridge_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await widget.getByRole("button", { name: "Invoke command" }).click();
	await expect(widget.getByTestId("command-result")).toHaveText("retpada | 3,2,1");
});

test("large binary state recovers a lost response and commits Python observer results", async ({
	page,
}) => {
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
});

test("large JSON updates preserve every record and Unicode text", async ({ page }) => {
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
});

test("child replacement renders the newly enrolled model", async ({ page }) => {
	await openWidget(page, "bridge_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.getByTestId("child-value")).toHaveText("7");
	await widget.getByRole("button", { name: "Replace child", exact: true }).click();
	await expect(widget.getByTestId("child-value")).toHaveText("11");
});

test("recursive references share a protocol child and replace its graph", async ({ page }) => {
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
});

test("source updates replace CSS and JavaScript in the live view", async ({ page }) => {
	await openWidget(page, "hot_reload_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.getByTestId("hot-label")).toHaveText("first generation");
	await expect(widget.getByTestId("hot-label")).toHaveCSS("color", "rgb(220, 38, 38)");
	await widget.getByRole("button", { name: "Change CSS" }).click();
	await expect(widget.getByTestId("hot-label")).toHaveCSS("color", "rgb(37, 99, 235)");
	await widget.getByRole("button", { name: "Change ESM" }).click();
	await expect(widget.getByTestId("hot-label")).toHaveText("second generation");
	await expect(widget.getByTestId("hot-label")).toHaveCSS("color", "rgb(37, 99, 235)");
});

test("a new app reuses verified shared sources from browser cache", async ({ page }) => {
	await openWidget(page, "large_asset_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.getByTestId("large-asset-status")).toHaveText("ready");
	await expect(widget.getByTestId("large-asset-leaf")).toHaveText([
		"first large source",
		"second large source",
	]);
	// Read from the host document to observe persistence across app documents.
	await expect
		.poll(() =>
			page.evaluate(async () => {
				const texts = await Promise.all(
					(await caches.keys()).map(async (name) => {
						const cache = await caches.open(name);
						return Promise.all(
							(await cache.keys()).map(async (key) => {
								const response = await cache.match(key);
								return response?.text() ?? "";
							}),
						);
					}),
				);
				return texts
					.flat()
					.map((text) => new TextEncoder().encode(text).byteLength)
					.sort((a, b) => a - b);
			}),
		)
		.toEqual([expect.any(Number), 3 * 1024 * 1024]);
	const requests = await page.getByLabel("Asset requests").innerText();
	expect(Number(requests)).toBeGreaterThan(0);
	await openWidget(page, "large_asset_probe");
	await expect(widget.getByTestId("large-asset-status")).toHaveText("ready");

	await expect(widget.getByTestId("large-asset-leaf")).toHaveText([
		"first large source",
		"second large source",
	]);
	await expect(page.getByLabel("Asset requests")).toHaveText(requests);
});

test("a rejected trait update reports an error and disposes the session", async ({ page }) => {
	await openWidget(page, "validation_probe");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await widget.getByRole("button", { name: "Send invalid value" }).click();
	await expect(widget.getByText(/expected an int/)).toBeVisible();
	await expect(page.getByLabel("Disposed sessions")).toHaveText("1");
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect(page.getByLabel("Python state")).toContainText('"isError":true');
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

test("factory sequences render in order and teardown revokes their state handle", async ({
	page,
}) => {
	await openWidget(page, "widget_group");
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.getByTestId("child-value")).toHaveText(["2", "5"]);
	await expect
		.poll(() => projectedState(page, "Model context"))
		.toEqual({ widgets: [{ value: 2 }, { value: 5 }] });
	await page.getByRole("button", { name: "Close widget", exact: true }).click();
	await expect(page.getByLabel("Host status")).toHaveText("Closed");
	await expect(page.getByLabel("Disposed sessions")).toHaveText("1");
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect(page.getByLabel("Python state")).toContainText('"isError":true');
	await openWidget(page, "widget_group");
	await expect(widget.getByTestId("child-value")).toHaveText(["2", "5"]);
});

test("startup reconciles many source revisions and retains its initial event backlog", async ({
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
});

test("fitting projections preserve full collections, text, keys and deep values", async ({
	page,
}) => {
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
});
