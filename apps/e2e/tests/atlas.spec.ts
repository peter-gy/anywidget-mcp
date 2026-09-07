import { expect, projectedState, test } from "./fixtures";
import { PNG } from "pngjs";

test("Embedding Atlas renders and filters a million points after a lost transfer response", async ({
	page,
}, testInfo) => {
	const rows = 1_000_000;
	await page.goto("/?drop-read");
	await expect(page.getByLabel("Host status")).toHaveText("Ready");
	const gpu = await page.evaluate(async () => {
		const adapter = await navigator.gpu?.requestAdapter();
		return {
			available: Boolean(adapter),
			shaderF16: Boolean(adapter?.features.has("shader-f16")),
			vendor: adapter?.info.vendor,
			architecture: adapter?.info.architecture,
		};
	});
	await testInfo.attach("gpu-adapter", {
		body: JSON.stringify(gpu, null, 2),
		contentType: "application/json",
	});
	expect(
		gpu.available,
		"Atlas requires a WebGPU adapter. Configure Metal or a Vulkan driver.",
	).toBe(true);
	expect(gpu.shaderF16, "Atlas requires shader-f16 support from the WebGPU adapter.").toBe(true);
	await page.getByLabel("Widget tool").selectOption("embedding_atlas");
	await page.getByLabel("Tool arguments").fill(JSON.stringify({ rows }));
	await page.getByRole("button", { name: "Open widget", exact: true }).click();
	await expect(page.getByLabel("Host status")).toHaveText("Connected");
	const widget = page.frameLocator('iframe[title="Widget"]');
	const total = rows.toLocaleString("en-US");
	await expect(widget.getByText(`${total} points`, { exact: true })).toBeVisible();
	const canvas = widget.locator("canvas").first();
	let coloredPixels = 0;
	await expect
		.poll(
			async () => {
				const pixels = PNG.sync.read(await canvas.screenshot()).data;
				let colored = 0;
				for (let index = 0; index < pixels.length; index += 4) {
					const r = pixels[index]!;
					const g = pixels[index + 1]!;
					const b = pixels[index + 2]!;
					if (pixels[index + 3]! > 0 && Math.max(r, g, b) - Math.min(r, g, b) > 24) colored++;
				}
				coloredPixels = colored;
				return colored / (pixels.length / 4);
			},
			{ message: "Embedding canvas contains rendered point or density colors" },
		)
		.toBeGreaterThan(0.01);
	await expect
		.poll(() => projectedState(page, "Model context"))
		.toMatchObject({ selected_count: rows });

	await testInfo.attach("atlas-rendered", {
		body: await page.screenshot({ fullPage: true }),
		contentType: "image/png",
	});
	await widget.getByRole("button", { name: "First 1000 id < 1000" }).click();
	await expect(widget.getByText(`1,000 / ${total} points`, { exact: true })).toBeVisible();
	await expect
		.poll(() => projectedState(page, "Model context"))
		.toMatchObject({
			selected_count: 1000,
			predicate: expect.stringContaining("id < 1000"),
		});
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect
		.poll(() => projectedState(page, "Python state"))
		.toMatchObject({
			selected_count: 1000,
			predicate: expect.stringContaining("id < 1000"),
		});

	await widget.getByRole("button", { name: "Clear filters", exact: true }).click();
	await expect(widget.getByText(`${total} / ${total} points`, { exact: true })).toBeVisible();
	await expect
		.poll(() => projectedState(page, "Model context"))
		.toEqual({ selected_count: rows, predicate: null });
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect
		.poll(() => projectedState(page, "Python state"))
		.toEqual({ selected_count: rows, predicate: null });

	const metrics = {
		rows,
		coloredPixels,
		maxToolRequestBytes: Number(await page.getByLabel("Largest tool request bytes").textContent()),
		maxToolResultBytes: Number(await page.getByLabel("Largest tool result bytes").textContent()),
		maxRawAttachmentBytes: Number(
			await page.getByLabel("Largest raw attachment bytes").textContent(),
		),
		attachmentReads: Number(await page.getByLabel("Asset requests").textContent()),
		lostReadResponses: Number(await page.getByLabel("Lost read responses").textContent()),
	};
	expect(metrics.maxToolRequestBytes).toBeLessThanOrEqual(128 * 1024);
	expect(metrics.maxToolResultBytes).toBeLessThanOrEqual(128 * 1024);
	expect(metrics.maxRawAttachmentBytes).toBeGreaterThan(8_000_000);
	expect(metrics.lostReadResponses).toBe(1);
	await testInfo.attach("wire-metrics", {
		body: JSON.stringify(metrics, null, 2),
		contentType: "application/json",
	});
});
