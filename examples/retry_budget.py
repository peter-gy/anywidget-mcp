import anywidget
import traitlets


class RetryBudget(anywidget.AnyWidget):
    _esm = """
    function render({ model, el, signal }) {
      el.classList.add("retry-budget");
      el.innerHTML = `
        <header><strong>Retry budget</strong><span data-summary></span></header>
        <label>Attempts <output data-attempts-value></output>
          <input data-attempts type="range" min="2" max="6" step="1">
        </label>
        <label>Base delay <output data-base-value></output>
          <input data-base type="range" min="0.1" max="2" step="0.1">
        </label>
        <div data-schedule></div>`;

      const attempts = el.querySelector("[data-attempts]");
      const base = el.querySelector("[data-base]");

      function draw() {
        const count = model.get("attempts");
        const delay = model.get("base_delay");
        const waits = Array.from({ length: count }, (_, index) =>
          index === 0 ? 0 : delay * 2 ** (index - 1));
        const total = waits.reduce((sum, wait) => sum + wait, 0);
        attempts.value = count;
        base.value = delay;
        el.querySelector("[data-attempts-value]").textContent = count;
        el.querySelector("[data-base-value]").textContent = `${delay.toFixed(1)} s`;
        el.querySelector("[data-summary]").textContent = `${total.toFixed(1)} s total wait`;
        const max = Math.max(...waits, 0.1);
        el.querySelector("[data-schedule]").innerHTML = waits.map((wait, index) => `
          <div class="attempt"><span>Attempt ${index + 1}</span>
            <i style="width:${Math.max(3, 100 * wait / max)}%"></i>
            <output>${index ? `+${wait.toFixed(1)} s` : "now"}</output></div>`
        ).join("");
      }

      function update(name, value) {
        const number = Number(value);
        const count = name === "attempts" ? number : model.get("attempts");
        const delay = name === "base_delay" ? number : model.get("base_delay");
        model.set(name, number);
        model.set("total_wait", delay * (2 ** (count - 1) - 1));
        model.save_changes();
      }

      attempts.addEventListener("input", (event) =>
        update("attempts", event.target.value), { signal });
      base.addEventListener("input", (event) =>
        update("base_delay", event.target.value), { signal });
      model.on("change:attempts change:base_delay", draw);
      signal.addEventListener("abort", () =>
        model.off("change:attempts change:base_delay", draw), { once: true });
      draw();
    }
    export default { render };
    """

    _css = """
    .retry-budget {
      max-width: 520px; padding: 16px; border: 1px solid #d8e0eb;
      border-radius: 12px; background: #fff; color: #172033;
      font: 13px/1.4 ui-sans-serif, system-ui, sans-serif;
    }
    .retry-budget header {
      display: flex; justify-content: space-between; margin-bottom: 14px;
    }
    .retry-budget header strong { font-size: 16px; }
    .retry-budget header span, .retry-budget output { color: #596579; }
    .retry-budget label {
      display: grid; grid-template-columns: 1fr auto; gap: 6px; margin: 12px 0;
    }
    .retry-budget input {
      grid-column: 1 / -1; width: 100%; accent-color: #315efb;
    }
    .retry-budget .attempt {
      display: grid; grid-template-columns: 72px 1fr 58px;
      align-items: center; gap: 10px; margin-top: 8px;
    }
    .retry-budget .attempt i {
      height: 8px; border-radius: 4px; background: #315efb;
    }
    .retry-budget .attempt output { text-align: right; }
    @media (prefers-color-scheme: dark) {
      .retry-budget {
        background: #151b26; color: #edf2f8; border-color: #354052;
      }
      .retry-budget header span, .retry-budget output { color: #aeb8c8; }
    }
    """

    attempts = traitlets.Int(4).tag(sync=True)
    base_delay = traitlets.Float(0.5).tag(sync=True)
    total_wait = traitlets.Float(3.5).tag(sync=True)
