import { abortable, withTimeout } from "../abort";
import type { Host, WidgetDefinition } from "../binding";
import type { AnyModel } from "../model";
import { isRecord, isString, type RuntimeRecord, type WidgetValue } from "../runtime-value";
import { exposeModel } from "./instance";
import { acquireTools, toolDocument, type ToolLease } from "./registry";
import { requestQueue } from "./request";
import { connectionSignal } from "./host";

interface CreationDescriptor {
	id: string;
	name: string;
	title: string;
	description: string;
	inputSchema: RuntimeRecord;
}
interface Child {
	controller: AbortController;
	model: Promise<AnyModel>;
	exposing: boolean;
	exposureVersion: number;
	exposure?: ReturnType<typeof exposeModel>;
	ready: Promise<void>;
	registration: Promise<void>;
	rendered?: Promise<void>;
	el?: HTMLElement;
}

export const session: WidgetDefinition = {
	render({ model, el, host, signal }) {
		const view = new SessionView(model, el, host, signal);
		return () => view.close();
	},
};

class SessionView {
	private readonly controller = new AbortController();
	private readonly signal: AbortSignal;
	private readonly connection: AbortSignal;
	private readonly children = new Map<string, Child>();
	private readonly factories = new Map<string, ToolLease>();
	private readonly status: HTMLElement;
	private readonly output: HTMLElement;
	private readonly events = [
		"_webmcp_active",
		"_webmcp_catalog",
		"_webmcp_widgets",
		"_webmcp_created",
	];
	private readonly target;
	private readonly request;
	private revision = 0;

	constructor(
		private readonly model: AnyModel,
		private readonly el: HTMLElement,
		private readonly host: Host,
		signal: AbortSignal,
	) {
		this.signal = AbortSignal.any([signal, this.controller.signal]);
		this.connection = connectionSignal(model, this.signal);
		this.target = toolDocument(el.ownerDocument);
		this.request = requestQueue(model, this.connection);
		this.status = el.ownerDocument.createElement("div");
		this.status.setAttribute("role", "status");
		this.status.style.cssText =
			"font: 13px system-ui; color: inherit; opacity: 0.7; padding: 4px 0;";
		this.output = el.ownerDocument.createElement("div");
		this.output.style.cssText = "display: grid; gap: 12px;";
		el.append(this.status, this.output);
		this.signal.addEventListener("abort", this.close, { once: true });
		this.connection.addEventListener("abort", this.refresh, { once: true });
		if (!this.signal.aborted) {
			for (const event of this.events) model.on(`change:${event}`, this.refresh);
			this.refresh();
		} else this.close();
	}

	readonly close = (): void => {
		this.signal.removeEventListener("abort", this.close);
		this.connection.removeEventListener("abort", this.refresh);
		this.controller.abort();
		for (const event of this.events) this.model.off(`change:${event}`, this.refresh);
		for (const lease of this.factories.values()) lease.release();
		this.factories.clear();
		for (const child of this.children.values()) child.controller.abort();
		this.children.clear();
		this.status.remove();
		this.output.remove();
	};

	private readonly refresh = (): void => {
		if (this.signal.aborted) return;
		const revision = ++this.revision;
		try {
			const connected = !this.connection.aborted;
			const active = connected && this.model.get("_webmcp_active") === true;
			const catalog = active && this.target ? readCatalog(this.model.get("_webmcp_catalog")) : [];
			const retained = new Set(catalog.map((item) => item.id));
			for (const [id, lease] of this.factories) {
				if (!retained.has(id)) {
					lease.release();
					this.factories.delete(id);
				}
			}
			for (const descriptor of catalog) this.registerFactory(descriptor);
			const exposed = active ? readRefs(this.model.get("_webmcp_widgets")) : [];
			const created = readRefs(this.model.get("_webmcp_created"));
			const refs = new Set([...exposed, ...created]);
			for (const [ref, child] of this.children) {
				if (!refs.has(ref)) {
					child.controller.abort();
					child.el?.remove();
					this.children.delete(ref);
				}
			}
			const pending = Array.from(this.factories.values(), (lease) => lease.ready());
			for (const ref of refs) {
				pending.push(this.child(ref, created.includes(ref), active).ready);
			}
			this.status.textContent = !connected
				? "WebMCP disconnected"
				: !active
					? "WebMCP disabled"
					: this.target
						? "Preparing WebMCP"
						: "WebMCP requires a browser with WebMCP enabled for this page.";
			void Promise.all(pending).then(
				() => {
					if (revision === this.revision && !this.signal.aborted && active && this.target) {
						this.status.textContent = "WebMCP ready";
					}
				},
				(error) => {
					if (revision === this.revision)
						this.reportError(error instanceof Error ? error : String(error));
				},
			);
		} catch (error) {
			this.reportError(error instanceof Error ? error : String(error));
		}
	};

	private readonly reportError = (error: Error | string): void => {
		if (this.signal.aborted || this.connection.aborted) return;
		this.status.textContent = error instanceof Error ? error.message : error;
		console.warn("Could not prepare AnyWidget WebMCP session.", error);
	};

	private registerFactory(descriptor: CreationDescriptor): void {
		if (!this.target) return;
		const build = (lifetime: AbortSignal) => {
			return [
				{
					name: descriptor.name,
					title: descriptor.title,
					description: descriptor.description,
					inputSchema: descriptor.inputSchema,
					annotations: { readOnlyHint: false },
					execute: async (input: RuntimeRecord, options: { signal?: AbortSignal }) => {
						const signal = options?.signal ? AbortSignal.any([lifetime, options.signal]) : lifetime;
						const result = await this.request(
							{ operation: "create", target: descriptor.id, arguments: input },
							signal,
						);
						if (!isString(result.ref) || !isString(result.widget_id) || !isRecord(result.tools)) {
							throw new Error("Python returned an invalid WebMCP creation result.");
						}
						const child = this.child(result.ref, true, true);
						await abortable(child.ready, signal);
						if (!child.exposure)
							throw new Error("The created widget is no longer exposed to WebMCP.");
						await abortable(child.exposure.ready(true), signal);
						return result;
					},
				},
			];
		};
		const lease = this.factories.get(descriptor.id);
		if (lease) lease.update(JSON.stringify(descriptor), build);
		else {
			const next = acquireTools(
				this.target,
				`factory:${descriptor.id}`,
				JSON.stringify(descriptor),
				build,
			);
			this.factories.set(descriptor.id, next);
		}
	}

	private child(ref: string, render: boolean, expose: boolean): Child {
		let child = this.children.get(ref);
		if (!child) {
			const controller = new AbortController();
			const signal = AbortSignal.any([this.signal, controller.signal]);
			const model = withTimeout(
				abortable(this.host.getModel(ref), signal),
				10_000,
				`Timed out waiting for WebMCP widget ${ref}.`,
			).catch((error) => {
				controller.abort(error);
				throw error;
			});
			child = {
				controller,
				model,
				exposing: false,
				exposureVersion: 0,
				ready: Promise.resolve(),
				registration: Promise.resolve(),
			};
			this.children.set(ref, child);
		}
		if (child.exposing !== expose) {
			child.exposing = expose;
			const version = ++child.exposureVersion;
			child.exposure?.release();
			child.exposure = undefined;
			child.registration = Promise.resolve();
			if (expose) {
				const current = child;
				const signal = AbortSignal.any([this.connection, child.controller.signal]);
				const registration = child.model.then(async (model) => {
					signal.throwIfAborted();
					if (!current.exposing || current.exposureVersion !== version) return;
					current.exposure = exposeModel(model, this.el.ownerDocument, signal);
					await current.exposure.ready();
				});
				child.registration = registration;
			}
		}
		if (render && !child.el) {
			const current = child;
			const signal = AbortSignal.any([this.signal, current.controller.signal]);
			const el = this.el.ownerDocument.createElement("div");
			child.el = el;
			this.output.append(el);
			child.rendered = child.model.then(async () => {
				try {
					signal.throwIfAborted();
					const rendering = abortable(this.host.getWidget(ref), signal).then((widget) => {
						signal.throwIfAborted();
						return abortable(widget.render({ el, signal }), signal);
					});
					await withTimeout(rendering, 10_000, `Timed out rendering WebMCP widget ${ref}.`);
				} catch (error) {
					current.controller.abort(error);
					el.remove();
					throw error;
				}
			});
		}
		child.ready = Promise.all([child.model, child.registration, child.rendered]).then(
			() => undefined,
		);
		return child;
	}
}

function readRefs(value: WidgetValue): string[] {
	if (!Array.isArray(value) || !value.every(isString))
		throw new Error("WebMCP widget references must be a list of strings.");
	return value;
}

function readCatalog(value: WidgetValue): CreationDescriptor[] {
	if (!Array.isArray(value)) throw new Error("WebMCP creation tools must be a list.");
	return value.map((item: WidgetValue) => {
		if (
			!isRecord(item) ||
			!isString(item.id) ||
			!isString(item.name) ||
			!isString(item.title) ||
			!isString(item.description) ||
			!isRecord(item.inputSchema)
		) {
			throw new Error("WebMCP creation tools require identity and an input schema.");
		}
		return {
			id: item.id,
			name: item.name,
			title: item.title,
			description: item.description,
			inputSchema: item.inputSchema,
		};
	});
}
