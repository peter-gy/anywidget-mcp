import {
	App,
	applyDocumentTheme,
	applyHostFonts,
	applyHostStyleVariables,
	type McpUiHostContext,
} from "@modelcontextprotocol/ext-apps";

import "./app.css";
import { beginRuntimeReplacement } from "./runtime-replacement";
import { abortable, randomId, RUNTIME_LIFECYCLE_TIMEOUT_MS } from "./runtime-lifecycle";
import { requiredString } from "./runtime-payload";
import {
	loadWidgetRuntime,
	parseToolLaunch,
	ToolResultGate,
	type ToolLaunch,
} from "./runtime-results";
import { disposeServerSession, WidgetRuntime } from "./runtime";
import {
	DEFAULT_LOADING_MESSAGE,
	loadingMessageForTool,
	loadingMessageFromArguments,
	loadingMessageFromResult,
	toolResultError,
} from "./status";
import { ToolCallQueue } from "./tool-calls";
import { mountDetachedWidget } from "./widget-mount";

export { disposeServerSession, WidgetRuntime };

declare const __ANYWIDGET_MCP_VERSION__: string;

let shell: HTMLElement;
let status: HTMLElement;
let root: HTMLElement;
let app: App;
let calls: ToolCallQueue;
let runtime: WidgetRuntime | undefined;
let connected: Promise<void>;
let pendingAttempt:
	| {
			controller: AbortController;
			promise: Promise<WidgetRuntime>;
	  }
	| undefined;
let loadingMessage = DEFAULT_LOADING_MESSAGE;
let renderRequest = Promise.resolve();
const resultGate = new ToolResultGate();

function startApp(): void {
	shell = getElement("app-shell");
	status = getElement("status");
	root = getElement("widget-root");
	app = new App(
		{ name: "anywidget MCP App", version: __ANYWIDGET_MCP_VERSION__ },
		{},
		{ autoResize: true, strict: true },
	);
	calls = new ToolCallQueue(app);
	let resolveConnected!: () => void;
	let rejectConnected!: (cause: unknown) => void;
	connected = new Promise<void>((resolve, reject) => {
		resolveConnected = resolve;
		rejectConnected = reject;
	});
	void connected.catch(() => undefined);

	// Ext Apps delivers the first tool result during connect, so handlers must be
	// installed before the bridge starts receiving host notifications.
	app.addEventListener("toolresult", (result) => {
		const resultError = toolResultError(result);
		if (resultError) {
			if (!resultGate.acceptedLaunch) queueResultError(new Error(resultError));
			return;
		}
		const launch = parseToolLaunch(result);
		if (launch.kind === "other") {
			if (!resultGate.acceptedLaunch) {
				queueResultError(new Error("Widget host did not preserve launch metadata"));
			}
			return;
		}
		if (launch.kind === "malformed") {
			if (!resultGate.acceptedLaunch) queueResultError(launch.error);
			return;
		}
		resultGate.deliver(launch.bootstrapId, (controller) => {
			renderRequest = renderRequest
				.then(() => mountToolResult(launch, controller))
				.catch(showError);
		});
	});
	app.addEventListener("toolinput", ({ arguments: args }) => {
		if (resultGate.acceptedLaunch) return;
		loadingMessage =
			loadingMessageFromArguments(args) ??
			loadingMessageForTool(app.getHostContext()?.toolInfo?.tool.title);
		showLoadingStatus();
	});
	app.addEventListener("toolcancelled", (params) => {
		if (resultGate.acceptedLaunch) return;
		resultGate.abort(new DOMException("Widget call cancelled", "AbortError"));
		showStatus(params.reason ? `Widget call cancelled: ${params.reason}` : "Widget call cancelled");
		setBusy(false);
	});
	app.addEventListener("hostcontextchanged", applyHostContext);
	app.onerror = (error) => showError(error);
	app.onteardown = async () => {
		resultGate.abort(new DOMException("Widget app is closing", "AbortError"));
		// A queued replacement can publish a runtime after the first disposal.
		// Drain the render queue, then close anything that it committed.
		await disposeRuntime();
		await renderRequest;
		await disposeRuntime();
		return {};
	};

	void app
		.connect()
		.then(() => {
			resolveConnected();
			const context = app.getHostContext();
			if (context) {
				applyHostContext(context);
				if (loadingMessage === DEFAULT_LOADING_MESSAGE) {
					loadingMessage = loadingMessageForTool(context.toolInfo?.tool.title);
					showLoadingStatus();
				}
			}
		})
		.catch((error) => {
			rejectConnected(error);
			showError(error);
		});
}

if ("document" in globalThis) startApp();

async function mountToolResult(launch: ToolLaunch, controller: AbortController): Promise<void> {
	if (launch.kind !== "bootstrap") return;
	let sessionHandle = launch.bootstrapId;
	const bootstrapOperationId = randomId();
	const replacement = beginRuntimeReplacement(
		(signal) => disposeRuntime(signal),
		async (signal) => {
			let runtimeCreationStarted = false;
			try {
				const materialized = await loadWidgetRuntime(
					launch,
					(name, args) => calls.callNow(name, args, signal),
					bootstrapOperationId,
					signal,
				);
				signal.throwIfAborted();
				// Disposal starts with the bootstrap capability, then switches to the
				// established session ID once bootstrap materialization succeeds.
				sessionHandle = requiredString(materialized.payload.instanceId, "instance ID");
				loadingMessage = loadingMessageFromResult(materialized.result) ?? loadingMessage;
				showLoadingStatus();
				root.replaceChildren();
				root.hidden = true;
				runtimeCreationStarted = true;
				return await WidgetRuntime.create(
					materialized.payload,
					calls,
					app,
					connected,
					undefined,
					signal,
					showError,
				);
			} catch (error) {
				// WidgetRuntime.create owns session cleanup once entered. Failures before
				// that boundary still need disposal through the bootstrap capability.
				if (!runtimeCreationStarted) {
					await disposeServerSession(
						calls,
						sessionHandle,
						"Timed out while disposing failed widget bootstrap",
						RUNTIME_LIFECYCLE_TIMEOUT_MS,
						bootstrapOperationId,
					).catch(() => undefined);
				}
				throw error;
			}
		},
		async () => {
			await disposeServerSession(
				calls,
				sessionHandle,
				"Timed out while disposing cancelled widget creation",
				RUNTIME_LIFECYCLE_TIMEOUT_MS,
				bootstrapOperationId,
			).catch(reportRuntimeError);
		},
		controller,
	);
	const { promise: creation } = replacement;
	pendingAttempt = replacement;
	let next: WidgetRuntime | undefined;
	try {
		const prepared = await creation;
		next = prepared;
		controller.signal.throwIfAborted();
		runtime = prepared;
		await mountDetachedWidget(
			root,
			(element) => abortable(prepared.mount(element, controller.signal), controller.signal),
			controller.signal,
		);
		root.hidden = false;
		status.hidden = true;
		setBusy(false);
	} catch (error) {
		if (next !== undefined) {
			if (runtime === next) runtime = undefined;
			await next.dispose(controller.signal.reason);
		}
		root.replaceChildren();
		root.hidden = true;
		if (controller.signal.aborted) return;
		throw error;
	} finally {
		if (pendingAttempt?.controller === controller) pendingAttempt = undefined;
	}
}

async function disposeRuntime(preserve?: AbortSignal): Promise<void> {
	// Replacement passes its own signal so disposing the current runtime cannot
	// abort the creation attempt that requested that disposal.
	const pending = pendingAttempt;
	if (pending && pending.controller.signal !== preserve) {
		pendingAttempt = undefined;
		pending.controller.abort(new DOMException("Widget runtime is closing", "AbortError"));
		const prepared = await pending.promise.catch(() => undefined);
		if (prepared) await prepared.dispose(pending.controller.signal.reason);
	}
	const current = runtime;
	runtime = undefined;
	if (current) await current.dispose();
	root.replaceChildren();
}

function applyHostContext(context: McpUiHostContext): void {
	if (context.theme) applyDocumentTheme(context.theme);
	if (context.styles?.variables) applyHostStyleVariables(context.styles.variables);
	if (context.styles?.css?.fonts) applyHostFonts(context.styles.css.fonts);

	const inset = context.safeAreaInsets;
	if (!inset) return;
	shell.style.paddingTop = `${inset.top}px`;
	shell.style.paddingRight = `${inset.right}px`;
	shell.style.paddingBottom = `${inset.bottom}px`;
	shell.style.paddingLeft = `${inset.left}px`;
}

function showStatus(message: string): void {
	status.hidden = false;
	status.dataset.kind = "status";
	status.setAttribute("role", "status");
	status.textContent = message;
}

function showLoadingStatus(): void {
	showStatus(loadingMessage);
	setBusy(true);
}

function setBusy(busy: boolean): void {
	shell.setAttribute("aria-busy", String(busy));
}

function showError(cause: unknown): void {
	const message = cause instanceof Error ? cause.message : String(cause);
	status.hidden = false;
	status.dataset.kind = "error";
	status.setAttribute("role", "alert");
	status.textContent = `Widget error: ${message}`;
	setBusy(false);
	console.error(cause);
}

function queueResultError(error: Error): void {
	resultGate.abort(new DOMException("Widget result failed", "AbortError"));
	renderRequest = renderRequest
		.then(async () => {
			await disposeRuntime().catch((disposeError) =>
				console.error("Failed to dispose widget after a tool error", disposeError),
			);
			root.hidden = true;
			showError(error);
		})
		.catch(showError);
}

function reportRuntimeError(cause: unknown): void {
	if (!("document" in globalThis)) console.error(cause);
	else showError(cause);
}

function getElement(id: string): HTMLElement {
	const element = document.getElementById(id);
	if (!element) throw new Error(`Missing element #${id}`);
	return element;
}
