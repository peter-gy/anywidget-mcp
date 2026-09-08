import { expect, test as base, type Page } from "@playwright/test";

export const test = base.extend({
	launchOptions: { args: ["--enable-blink-features=WebMCP"] },
	page: async ({ page }, use) => {
		const errors: string[] = [];
		page.on("pageerror", (error) => errors.push(error.message));
		page.on("console", (message) => {
			if (/Could not .*WebMCP/.test(message.text())) errors.push(message.text());
		});
		try {
			await use(page);
		} finally {
			expect(errors, "Uncaught errors or failed WebMCP preparation").toEqual([]);
		}
	},
});

export interface RegisteredTool {
	name: string;
	description: string;
	inputSchema: string;
}

interface NativeModelContext {
	getTools(): Promise<RegisteredTool[]>;
	// Playwright's pinned Chromium accepts serialized arguments at this API boundary.
	executeTool(tool: RegisteredTool, inputArguments: string): Promise<string>;
}

interface NativeDocument extends Document {
	readonly modelContext: NativeModelContext;
}

declare const document: NativeDocument;

export interface CounterInput {
	start?: number;
	value?: number;
	locked?: boolean;
	secret?: string;
	notes?: string;
	fail?: boolean;
}

export interface CounterResult {
	state: { value: number; doubled: number };
	widget_id?: string;
	tools?: { read: string; update?: string };
}

export async function registeredTools(page: Page): Promise<RegisteredTool[]> {
	return page.evaluate(async () => {
		const context = document.modelContext;
		return (await context.getTools()).map(({ name, description, inputSchema }) => ({
			name,
			description,
			inputSchema,
		}));
	});
}

export async function executeTool(
	page: Page,
	name: string,
	input: CounterInput = {},
): Promise<CounterResult> {
	return page.evaluate(
		async ({ name, input }) => {
			const context = document.modelContext;
			const tools = await context.getTools();
			const tool = tools.find((candidate) => candidate.name === name);
			if (!tool) throw new Error(`WebMCP tool ${name} is unavailable`);
			return JSON.parse(await context.executeTool(tool, JSON.stringify(input)));
		},
		{ name, input },
	);
}
