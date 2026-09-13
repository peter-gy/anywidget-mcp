import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import {
	App,
	applyDocumentTheme,
	applyHostFonts,
	applyHostStyleVariables,
	type McpUiHostContext,
} from "@modelcontextprotocol/ext-apps";

import "./app.css";
import { beginRuntimeReplacement } from "./runtime-replacement";
import { abortable } from "./abort";
import { randomId, RUNTIME_LIFECYCLE_TIMEOUT_MS } from "./runtime-lifecycle";
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
	loadingMessageFromPayload,
	toolResultError,
	toolResultFailure,
	SessionUnavailableError,
	ReopenError,
	RuntimeStoppedError,
} from "./status";
import { ToolCallQueue } from "./tool-calls";
import { mountDetachedWidget } from "./widget-mount";

export { disposeServerSession, WidgetRuntime };

declare const __ANYWIDGET_MCP_VERSION__: string;

let shell: HTMLElement;
let status: HTMLElement;
let root: HTMLElement;
let reopenButton: HTMLButtonElement | undefined;
let currentLaunch: Extract<ToolLaunch, { kind: "bootstrap" }> | undefined;
let reopenAttempt: AbortController | undefined;
let reopenTask: Promise<void> | undefined;
let autoReopened = false;
let closed = false;
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
	app.addEventListener("toolresult", receiveToolResult);
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
		closed = true;
		reopenAttempt?.abort(new DOMException("Widget app is closing", "AbortError"));
		await reopenTask;
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

function receiveToolResult(result: CallToolResult): void {
	const resultError = toolResultError(result);
	if (resultError) {
		if (!resultGate.acceptedLaunch) queueResultError(new Error(resultError));
		return;
	}
	const launch = parseToolLaunch(result);
	if (launch.kind === "other") {
		if (!resultGate.acceptedLaunch) {
			queueResultError(
				new Error(
					"Widget host did not preserve launch metadata. Reconnect the server or request a new widget in a compatible MCP Apps host.",
				),
			);
		}
		return;
	}
	if (launch.kind === "malformed") {
		if (!resultGate.acceptedLaunch) queueResultError(launch.error);
		return;
	}
	if (closed) {
		void disposeServerSession(
			calls,
			launch.bootstrapId,
			"Disposing late widget result",
			RUNTIME_LIFECYCLE_TIMEOUT_MS,
			randomId(),
		).catch(console.error);
		return;
	}
	resultGate.deliver(launch.bootstrapId, (controller) => {
		const recreated = reopenAttempt !== undefined;
		currentLaunch = launch;
		reopenAttempt?.abort(new DOMException("Widget result received", "AbortError"));
		setReopenVisible(false);
		renderRequest = renderRequest
			.then(() => mountToolResult(launch, controller, recreated))
			.catch(showError);
	});
}

async function mountToolResult(
	launch: ToolLaunch,
	controller: AbortController,
	recreated = false,
): Promise<void> {
	if (launch.kind !== "bootstrap") return;
	let sessionHandle = launch.bootstrapId;
	const bootstrapOperationId = randomId();
	const replacement = beginRuntimeReplacement(
		(signal) => disposeRuntime(signal),
		async (signal) => {
			let runtimeCreationStarted = false;
			try {
				const materialized = await calls.transaction(
					(call) => loadWidgetRuntime(launch, call, bootstrapOperationId, signal),
					signal,
				);
				signal.throwIfAborted();
				// Disposal starts with the bootstrap capability, then switches to the
				// established session ID once bootstrap materialization succeeds.
				sessionHandle = requiredString(materialized.payload.instanceId, "instance ID");
				loadingMessage = loadingMessageFromPayload(materialized.payload) ?? loadingMessage;
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
					(cause) => {
						if (!controller.signal.aborted) showRuntimeError(cause);
					},
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
		setReopenVisible(false);
		setBusy(false);
	} catch (error) {
		if (next !== undefined) {
			if (runtime === next) runtime = undefined;
			await next.dispose(controller.signal.reason);
		}
		root.replaceChildren();
		root.hidden = true;
		if (controller.signal.aborted) return;
		if (recreated && !(error instanceof SessionUnavailableError)) {
			showReopenError(
				new ReopenError(launch.reopen?.tool ?? "widget", error, "reopen_mount_failed"),
			);
			return;
		}
		if (error instanceof SessionUnavailableError && launch.reopen) {
			if (launch.reopen.mode === "auto" && !autoReopened) startReopen();
			else showUnavailable();
			return;
		}
		if (error instanceof SessionUnavailableError) {
			showError(
				new SessionUnavailableError(
					"Widget session is unavailable and this result has no supported reopening metadata. Ask for a new widget.",
				),
			);
			return;
		}
		throw new Error(
			`Unable to open widget. ${error instanceof Error ? error.message : String(error)}. Check the server connection and reload this result, or ask for a new widget.`,
			{ cause: error },
		);
	} finally {
		if (pendingAttempt?.controller === controller) pendingAttempt = undefined;
	}
}

function recoveryAction(): string {
	const descriptor = currentLaunch?.reopen;
	if (descriptor?.ui) return "Choose Reopen to start again from the original inputs.";
	if (descriptor?.mode === "auto")
		return "Reload this result to start again from the original inputs.";
	return "Ask for a new widget.";
}

function setReopenVisible(visible: boolean): void {
	const show = visible && currentLaunch?.reopen?.ui && !closed;
	if (!reopenButton) {
		if (!show) return;
		reopenButton = document.createElement("button");
		reopenButton.id = "reopen";
		reopenButton.textContent = "Reopen";
		reopenButton.addEventListener("click", startReopen);
		status.after(reopenButton);
	}
	reopenButton.hidden = !show;
}

function showUnavailable(): void {
	root.hidden = true;
	showStatus(`This widget session is no longer available. ${recoveryAction()}`);
	setBusy(false);
	setReopenVisible(true);
}

function showRuntimeError(cause: unknown): void {
	const reason = cause instanceof RuntimeStoppedError ? cause.cause : cause;
	if (reason instanceof SessionUnavailableError && currentLaunch?.reopen) showUnavailable();
	else {
		showError(cause);
		if (cause instanceof RuntimeStoppedError) {
			root.hidden = true;
			setReopenVisible(true);
		}
	}
}

function startReopen(): void {
	const descriptor = currentLaunch?.reopen;
	if (!descriptor || reopenAttempt || closed) return;
	const controller = new AbortController();
	reopenAttempt = controller;
	autoReopened = true;
	setReopenVisible(false);
	showLoadingStatus();
	// Creation has ordinary tools/call semantics. A lost response must not
	// silently rerun a factory, unlike replayable bootstrap and comm calls.
	reopenTask = calls
		.call(descriptor.tool, descriptor.arguments, controller.signal)
		.then((result) => {
			controller.signal.throwIfAborted();
			if (result.isError)
				throw new ReopenError(descriptor.tool, toolResultFailure(result), "reopen_rejected");
			const launch = parseToolLaunch(result);
			if (launch.kind !== "bootstrap")
				throw new ReopenError(
					descriptor.tool,
					"The tool returned no valid widget launch.",
					"reopen_mount_failed",
				);
			receiveToolResult(result);
		})
		.catch((error) => {
			if (controller.signal.aborted || closed) return;
			showReopenError(
				error instanceof ReopenError
					? error
					: new ReopenError(descriptor.tool, error, "reopen_unconfirmed"),
			);
		})
		.finally(() => {
			if (reopenAttempt === controller) reopenAttempt = undefined;
		});
}

function showReopenError(error: ReopenError): void {
	showError(error);
	setReopenVisible(true);
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
	delete status.dataset.errorCode;
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
	let message = cause instanceof Error ? cause.message : String(cause);
	if (cause instanceof ReopenError || cause instanceof RuntimeStoppedError) {
		message += `\nCheck the server connection and access permissions. ${recoveryAction()} If the problem persists, ask for a new widget with updated inputs.`;
	}
	status.hidden = false;
	status.dataset.kind = "error";
	status.dataset.errorCode =
		cause instanceof RuntimeStoppedError
			? "runtime_stopped"
			: cause instanceof ReopenError
				? cause.code
				: cause instanceof SessionUnavailableError
					? "session_unavailable"
					: "widget_error";
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
