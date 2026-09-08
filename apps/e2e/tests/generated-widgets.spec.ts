import { expect, projectedState, test } from "./fixtures";

const wasmWidget = `
import anywidget
import traitlets

class WasmWidget(anywidget.AnyWidget):
    answer = traitlets.Int(0).tag(sync=True)
    _esm = '''
    export default {
      async render({ model, el }) {
        const bytes = new Uint8Array([
          0,97,115,109,1,0,0,0,1,5,1,96,0,1,127,3,2,1,0,
          7,10,1,6,97,110,115,119,101,114,0,0,10,6,1,4,0,65,42,11
        ]);
        const { instance } = await WebAssembly.instantiate(bytes);
        const output = document.createElement("output");
        output.textContent = String(instance.exports.answer());
        model.set("answer", instance.exports.answer());
        model.save_changes();
        el.append(output);
      }
    };
    '''
`;

test("generated widgets execute WebAssembly with the declared script permission", async ({
	page,
}) => {
	await page.getByLabel("Widget tool").selectOption("create_anywidget");
	await page.getByLabel("Tool arguments").fill(JSON.stringify({ code: wasmWidget }));
	await page.getByRole("button", { name: "Open widget", exact: true }).click();
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.locator("output")).toHaveText("42");
	await expect.poll(() => projectedState(page, "Model context")).toEqual({ answer: 42 });
});

const staffingWidget = `
import math
import anywidget
import traitlets

class Staffing(anywidget.AnyWidget):
    demand = traitlets.Int(120).tag(sync=True)
    capacity = traitlets.Int(15).tag(sync=True)
    agents = traitlets.Int(10).tag(sync=True)
    error = traitlets.Unicode().tag(sync=True)
    _esm = '''
    export default { render({model, el}) {
      for (const name of ["demand", "capacity"]) {
        const label = document.createElement("label");
        label.textContent = name;
        const input = document.createElement("input");
        input.type = "number";
        input.value = model.get(name);
        input.addEventListener("change", () => {
          model.set(name, Number(input.value)); model.save_changes();
        });
        label.append(input); el.append(label);
      }
      const output = document.createElement("output");
      const draw = () => { output.textContent = model.get("error") || model.get("agents") + " agents"; };
      model.on("change:agents change:error", draw); draw(); el.append(output);
    }};
    '''

    @traitlets.observe("demand", "capacity")
    def compute(self, change):
        if self.capacity <= 0:
            self.error = "Capacity must be positive"
        else:
            self.error = ""
            self.agents = math.ceil(self.demand / (self.capacity * 0.8))
`;

test("generated widget observers validate inputs and recover after correction", async ({
	page,
}) => {
	await page.getByLabel("Widget tool").selectOption("create_anywidget");
	await page.getByLabel("Tool arguments").fill(JSON.stringify({ code: staffingWidget }));
	await page.getByRole("button", { name: "Open widget", exact: true }).click();
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.locator("output")).toHaveText("10 agents");
	await widget.getByLabel("demand").fill("240");
	await widget.getByLabel("demand").press("Tab");
	await expect(widget.locator("output")).toHaveText("20 agents");
	await widget.getByLabel("capacity").fill("0");
	await widget.getByLabel("capacity").press("Tab");
	await expect(widget.locator("output")).toHaveText("Capacity must be positive");
	await expect
		.poll(() => projectedState(page, "Model context"))
		.toMatchObject({
			demand: 240,
			capacity: 0,
			error: "Capacity must be positive",
		});
	await widget.getByLabel("capacity").fill("30");
	await widget.getByLabel("capacity").press("Tab");
	await expect(widget.locator("output")).toHaveText("10 agents");
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect
		.poll(() => projectedState(page, "Python state"))
		.toEqual({
			demand: 240,
			capacity: 30,
			agents: 10,
			error: "",
		});
});

test("selected generated classes share state and release their session", async ({ page }) => {
	const code = `${staffingWidget}\nclass LargerTeam(Staffing):\n    demand = traitlets.Int(240).tag(sync=True)\n    agents = traitlets.Int(20).tag(sync=True)\n`;
	await page.getByLabel("Widget tool").selectOption("create_anywidget");
	await page.getByLabel("Tool arguments").fill(
		JSON.stringify({
			code,
			classnames: ["LargerTeam", "Staffing"],
		}),
	);
	await page.getByRole("button", { name: "Open widget", exact: true }).click();
	const widget = page.frameLocator('iframe[title="Widget"]');
	await expect(widget.locator("output")).toHaveText(["20 agents", "10 agents"]);
	await widget.getByLabel("demand", { exact: true }).first().fill("360");
	await widget.getByLabel("demand", { exact: true }).first().press("Tab");
	await expect(widget.locator("output")).toHaveText(["30 agents", "10 agents"]);
	await expect
		.poll(() => projectedState(page, "Model context"))
		.toEqual({
			widgets: [
				{ demand: 360, capacity: 15, agents: 30, error: "" },
				{ demand: 120, capacity: 15, agents: 10, error: "" },
			],
		});
	await page.getByRole("button", { name: "Close widget", exact: true }).click();
	await expect(page.getByLabel("Host status")).toHaveText("Closed");
	await expect(page.getByLabel("Disposed sessions")).toHaveText("1");
	await page.getByRole("button", { name: "Read Python state" }).click();
	await expect(page.getByLabel("Python state")).toContainText('"isError":true');
	await page.getByRole("button", { name: "Open widget", exact: true }).click();
	await expect(widget.locator("output")).toHaveText(["20 agents", "10 agents"]);
});
