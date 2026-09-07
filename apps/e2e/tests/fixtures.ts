import { expect, test as base, type Page } from "@playwright/test";

export const test = base.extend({
	page: async ({ page }, use) => {
		const errors: string[] = [];
		page.on("pageerror", (error) => errors.push(error.message));
		await page.goto("/");
		await expect(page.getByLabel("Host status")).toHaveText("Ready");
		try {
			await use(page);
		} finally {
			const close = page.getByRole("button", { name: "Close widget", exact: true });
			if (await close.isEnabled()) {
				await close.click();
				await expect(page.getByLabel("Host status")).toHaveText("Closed");
			}
			expect(errors, "Uncaught errors from the host or widget").toEqual([]);
		}
	},
});

export { expect } from "@playwright/test";

export async function projectedState(page: Page, label: "Model context" | "Python state") {
	const text = await page.getByLabel(label).textContent();
	if (!text) return undefined;
	const result = JSON.parse(text);
	return label === "Model context" ? result.state : result.structuredContent?.state;
}
