import {
	App,
	applyDocumentTheme,
	applyHostFonts,
	applyHostStyleVariables,
	type McpUiHostContext,
} from "@modelcontextprotocol/ext-apps";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";
import "./app.css";
import {
	WidgetBinding,
	type Experimental,
	type Host,
	type InitializeProtocolScope,
	type RuntimeBinding,
} from "./binding";
import { ModelContextSync, type ModelContextSnapshot } from "./context";
import {
	BridgeModel,
	insertBuffers,
	scopedModel,
	serializeCustom,
	serializeUpdate,
	type CommData,
	type JsonPath,
	type ModelPayload,
	type State,
} from "./model";
import { ToolCallQueue, type QueuedToolCall } from "./tool-calls";

interface RawModelPayload {
	modelId?: string;
	state?: State;
	buffers?: unknown[];
	bufferPaths?: JsonPath[];
}

interface RawRuntimePayload {
	instanceId?: string;
	rootModelId?: string;
	models?: Record<string, RawModelPayload>;
	messages?: unknown;
	context?: ModelContextSnapshot;
}

interface RawCommMessage {
	modelId?: string;
	data?: CommData;
	buffers?: unknown[];
}

let shell: HTMLElement;
let status: HTMLElement;
let root: HTMLElement;
let app: App;
let calls: ToolCallQueue;
let runtime: WidgetRuntime | undefined;
let connected: Promise<void>;

function startApp(): void {
	shell = getElement<HTMLElement>("app-shell");
	status = getElement<HTMLElement>("status");
	root = getElement<HTMLElement>("widget-root");
	app = new App(
		{ name: "anywidget MCP App", version: "0.1.0" },
		{},
		{ autoResize: true, strict: true },
	);
	calls = new ToolCallQueue(app);
	let renderRequest = Promise.resolve();
	let resolveConnected!: () => void;
	let rejectConnected!: (error: unknown) => void;
	connected = new Promise<void>((resolve, reject) => {
		resolveConnected = resolve;
		rejectConnected = reject;
	});
	void connected.catch(() => undefined);

	// Ext Apps delivers the first tool result during connect, so handlers must be
	// installed before the bridge starts receiving host notifications.
	app.addEventListener("toolresult", (result) => {
		renderRequest = renderRequest.then(() => mountToolResult(result)).catch(showError);
	});
	app.addEventListener("toolcancelled", (params) => {
		showStatus(params.reason ? `Widget call cancelled: ${params.reason}` : "Widget call cancelled");
	});
	app.addEventListener("hostcontextchanged", applyHostContext);
	app.onerror = (error) => showError(error);
	app.onteardown = async () => {
		await disposeRuntime();
		return {};
	};

	void app
		.connect()
		.then(() => {
			resolveConnected();
			const context = app.getHostContext();
			if (context) applyHostContext(context);
		})
		.catch((error) => {
			rejectConnected(error);
			showError(error);
		});
}

if (typeof document !== "undefined") startApp();

async function mountToolResult(result: CallToolResult): Promise<void> {
	const payload = runtimePayload(result);
	if (!payload) return;

	await disposeRuntime();
	showStatus("Loading widget…");
	root.replaceChildren();
	root.hidden = true;

	const next = new WidgetRuntime(payload, calls, app, connected);
	runtime = next;
	try {
		await next.mount(root);
	} catch (error) {
		if (runtime === next) runtime = undefined;
		await next.dispose();
		throw error;
	}
	if (runtime !== next) return;

	root.hidden = false;
	status.hidden = true;
}

async function disposeRuntime(): Promise<void> {
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
	status.textContent = message;
}

function showError(error: unknown): void {
	const message = error instanceof Error ? error.message : String(error);
	status.hidden = false;
	status.dataset.kind = "error";
	status.textContent = `Widget error: ${message}`;
	console.error(error);
}

function reportRuntimeError(error: unknown): void {
	if (typeof document === "undefined") console.error(error);
	else showError(error);
}

type BindingFactory = (runtime: WidgetRuntime, model: BridgeModel) => RuntimeBinding;
const RUNTIME_LIFECYCLE_TIMEOUT_MS = 3000;
const TRANSPORT_RETRY_DELAYS_MS = [100, 200] as const;
const POLL_ACTIVE_DELAY_MS = 500;
const POLL_IDLE_DELAYS_MS = [POLL_ACTIVE_DELAY_MS, 1000, 2000, 5000, 10_000, 15_000] as const;

interface UpdateProtocolOperation {
	kind: "update";
	sequence: number;
	model: BridgeModel;
	state: Map<string, unknown>;
	operationId: string;
}

interface CustomProtocolOperation {
	kind: "custom";
	sequence: number;
	model: BridgeModel;
	data: CommData;
	buffers: string[];
	operationId: string;
	resolve?: () => void;
	reject?: (error: unknown) => void;
}

interface PollProtocolOperation {
	kind: "poll";
	sequence: number;
	operationId: string;
	resolve(messageCount: number): void;
	reject(error: unknown): void;
}

type ProtocolOperation = UpdateProtocolOperation | CustomProtocolOperation | PollProtocolOperation;

export class WidgetRuntime {
	readonly instanceId: string;
	readonly rootModelId: string;
	readonly models = new Map<string, BridgeModel>();

	private readonly bindings = new Map<string, RuntimeBinding>();
	private readonly controller = new AbortController();
	private readonly contextSync: ModelContextSync;
	private readonly initialContext?: ModelContextSnapshot;
	private readonly createBinding: BindingFactory;
	private readonly protocolOperations: ProtocolOperation[] = [];
	private protocolSequence = 0;
	private protocolTask?: Promise<void>;
	private activeProtocolCall?: QueuedToolCall;
	private disposeTask?: Promise<void>;
	private disposed = false;
	private pollTask?: Promise<void>;
	private pollActivityVersion = 0;
	private pollWake?: () => void;

	constructor(
		payload: RawRuntimePayload,
		private readonly calls: ToolCallQueue,
		app: App,
		connected: Promise<void>,
		createBinding?: BindingFactory,
	) {
		this.instanceId = requiredString(payload.instanceId, "instance ID");
		this.rootModelId = requiredString(payload.rootModelId, "root model ID");
		this.createBinding =
			createBinding ??
			((runtime, model) => new WidgetBinding(runtime, model, { reportError: showError }));

		for (const modelPayload of normalizeModels(payload.models)) {
			this.registerModel(modelPayload);
		}
		if (!this.models.has(this.rootModelId)) {
			throw new Error(`Root model ${this.rootModelId} is missing from the widget payload`);
		}
		this.applyLaunchMessages(payload.messages);
		this.contextSync = new ModelContextSync(app, connected, (error) =>
			console.warn("Failed to update model context", error),
		);
		this.initialContext = normalizeContext(payload.context);
	}

	async mount(element: HTMLElement): Promise<void> {
		const initialized = await Promise.allSettled(
			Array.from(this.bindings.values(), (binding) => binding.initialize()),
		);
		const failed = initialized.find(
			(result): result is PromiseRejectedResult => result.status === "rejected",
		);
		if (failed) throw failed.reason;
		this.controller.signal.throwIfAborted();
		await this.binding(this.rootModelId).render(element, this.controller.signal);
		if (this.initialContext) this.contextSync.enqueue(this.initialContext);
		this.pollTask = this.poll();
	}

	async send(
		modelId: string,
		data: CommData,
		buffers: string[],
		operationId: string,
	): Promise<void> {
		if (this.disposed) return;
		await this.callAndProcess("anywidget_comm", {
			instance_id: this.instanceId,
			model_id: modelId,
			data,
			buffers,
			operation_id: operationId,
		});
		this.noteProtocolActivity();
	}

	enqueueUpdate(model: BridgeModel, state: Map<string, unknown>): void {
		if (this.disposed || this.models.get(model.modelId) !== model) return;
		const tail = this.protocolOperations.at(-1);
		if (tail?.kind === "update" && tail.model === model) {
			for (const [key, value] of state) tail.state.set(key, value);
			return;
		}
		this.protocolOperations.push({
			kind: "update",
			sequence: ++this.protocolSequence,
			model,
			state,
			operationId: randomId(),
		});
		this.startProtocolOperations();
	}

	enqueueCustom(model: BridgeModel, data: CommData, buffers: string[]): void {
		if (this.disposed || this.models.get(model.modelId) !== model) return;
		this.appendCustom(model, data, buffers);
	}

	dispose(reason?: unknown): Promise<void> {
		if (this.disposeTask) return this.disposeTask;
		this.disposed = true;
		this.controller.abort(reason);
		this.cancelQueuedProtocolOperations(abortReason(this.controller.signal));
		const task = this.finishDispose();
		this.disposeTask = task;
		return task;
	}

	private async finishDispose(): Promise<void> {
		const disposeSignal = AbortSignal.timeout(3000);
		const disposeRequest = withTimeout(
			Promise.resolve().then(() =>
				this.calls.callNow("anywidget_dispose", { instance_id: this.instanceId }, disposeSignal),
			),
			3000,
			"Timed out while disposing widget session",
		).then(
			() => ({ error: undefined }),
			(error: unknown) => ({ error }),
		);
		await this.contextSync.dispose();
		await withTimeout(
			this.protocolTask ?? Promise.resolve(),
			RUNTIME_LIFECYCLE_TIMEOUT_MS,
			"Timed out while stopping widget protocol work",
		).catch((error) => console.error(error));

		await Promise.allSettled(
			Array.from(this.bindings.values(), (binding) => this.disposeBinding(binding)),
		);
		void this.pollTask?.catch(() => undefined);

		const { error } = await disposeRequest;
		if (error !== undefined) console.error("Failed to dispose widget session", error);
	}

	model(modelId: string): BridgeModel {
		const model = this.models.get(modelId);
		if (!model) throw new Error(`Unknown anywidget model ${modelId}`);
		return model;
	}

	binding(modelId: string): RuntimeBinding {
		const binding = this.bindings.get(modelId);
		if (!binding) throw new Error(`Unknown anywidget binding ${modelId}`);
		return binding;
	}

	host(signal: AbortSignal): Host {
		return {
			getModel: async (ref) => {
				signal.throwIfAborted();
				return scopedModel(this.model(parseWidgetRef(ref)), signal);
			},
			getWidget: async (ref) => {
				signal.throwIfAborted();
				const binding = this.binding(parseWidgetRef(ref));
				const exports = await binding.getExports();
				signal.throwIfAborted();
				return {
					exports,
					render: async ({ el, signal: childSignal }) => {
						const renderSignal = childSignal ? AbortSignal.any([signal, childSignal]) : signal;
						renderSignal.throwIfAborted();
						await binding.render(el, renderSignal);
						renderSignal.throwIfAborted();
					},
				};
			},
		};
	}

	experimental(
		model: BridgeModel,
		scopeSignal: AbortSignal,
		protocolScope?: InitializeProtocolScope,
	): Experimental {
		return {
			invoke: <T>(
				name: string,
				message?: unknown,
				options: { buffers?: DataView[]; signal?: AbortSignal } = {},
			): Promise<[T, DataView[]]> => {
				const id = randomId();
				const requestSignal = options.signal ?? AbortSignal.timeout(3000);
				const signal = AbortSignal.any([scopeSignal, requestSignal]);
				const result = new Promise<[T, DataView[]]>((resolve, reject) => {
					let settled = false;
					let response: [T, DataView[]] | undefined;
					let removeResponseHandler: () => void = () => undefined;
					const finish = (callback: () => void): void => {
						if (settled) return;
						settled = true;
						removeResponseHandler();
						signal.removeEventListener("abort", abort);
						callback();
					};
					const handler = (content: Record<string, unknown>, buffers: DataView[]) => {
						response = [content.response as T, buffers];
					};
					const abort = () => finish(() => reject(abortReason(signal)));

					if (signal.aborted) {
						abort();
						return;
					}
					signal.addEventListener("abort", abort, { once: true });
					removeResponseHandler = model.onCommandResponse(id, handler);
					const content = { id, kind: "anywidget-command", name, msg: message };
					try {
						const serialized = serializeCustom(content, options.buffers);
						let complete!: () => void;
						let fail!: (error: unknown) => void;
						const completion = new Promise<void>((resolveCompletion, rejectCompletion) => {
							complete = resolveCompletion;
							fail = rejectCompletion;
						});
						const operation = this.appendCustom(
							model,
							serialized.data,
							serialized.buffers,
							complete,
							fail,
						);
						if (!operation) throw abortReason(this.controller.signal);
						void completion.then(
							() => {
								const captured = response;
								if (captured) finish(() => resolve(captured));
								else finish(() => reject(new Error(`Command ${name} returned no response`)));
							},
							(error: unknown) => finish(() => reject(error)),
						);
						if (protocolScope?.active) {
							void this.runInitializeOperations(protocolScope, operation, signal).catch(
								() => undefined,
							);
						}
					} catch (error) {
						finish(() => reject(error));
					}
				});
				void result.catch(() => undefined);
				return result;
			},
		};
	}

	private async poll(): Promise<void> {
		let delayIndex = 0;
		let activityVersion = this.pollActivityVersion;
		while (!this.controller.signal.aborted) {
			try {
				if (activityVersion !== this.pollActivityVersion) {
					delayIndex = 0;
					activityVersion = this.pollActivityVersion;
				}
				const interrupted = await this.waitForPollDelay(
					POLL_IDLE_DELAYS_MS[delayIndex] ?? POLL_ACTIVE_DELAY_MS,
				);
				if (this.controller.signal.aborted) return;
				if (interrupted) {
					delayIndex = 0;
					activityVersion = this.pollActivityVersion;
					continue;
				}
				const messageCount = await this.enqueuePoll();
				if (!this.controller.signal.aborted) {
					delayIndex =
						messageCount > 0 ? 0 : Math.min(delayIndex + 1, POLL_IDLE_DELAYS_MS.length - 1);
				}
			} catch (error) {
				if (this.controller.signal.aborted) return;
				console.error("Failed to poll widget session", error);
			}
		}
	}

	private waitForPollDelay(milliseconds: number): Promise<boolean> {
		return new Promise((resolve) => {
			if (this.controller.signal.aborted) {
				resolve(false);
				return;
			}
			let settled = false;
			let timeout: ReturnType<typeof globalThis.setTimeout> | undefined;
			const finish = (interrupted: boolean): void => {
				if (settled) return;
				settled = true;
				if (timeout !== undefined) globalThis.clearTimeout(timeout);
				this.controller.signal.removeEventListener("abort", abort);
				if (this.pollWake === wake) this.pollWake = undefined;
				resolve(interrupted);
			};
			const abort = (): void => finish(false);
			const wake = (): void => finish(true);
			this.pollWake = wake;
			timeout = globalThis.setTimeout(() => finish(false), milliseconds);
			this.controller.signal.addEventListener("abort", abort, { once: true });
		});
	}

	private noteProtocolActivity(): void {
		this.pollActivityVersion += 1;
		this.pollWake?.();
	}

	private enqueuePoll(): Promise<number> {
		if (this.disposed) return Promise.reject(abortReason(this.controller.signal));
		return new Promise<number>((resolve, reject) => {
			this.protocolOperations.push({
				kind: "poll",
				sequence: ++this.protocolSequence,
				operationId: randomId(),
				resolve,
				reject,
			});
			this.startProtocolOperations();
		});
	}

	private startProtocolOperations(): void {
		if (
			this.disposed ||
			this.protocolTask ||
			this.activeProtocolCall ||
			this.protocolOperations.length === 0
		)
			return;
		const task = this.flushProtocolOperations();
		this.protocolTask = task;
		const finish = (): void => {
			if (this.protocolTask !== task) return;
			this.protocolTask = undefined;
			this.startProtocolOperations();
		};
		void task.then(finish, (error: unknown) => {
			if (!this.controller.signal.aborted) this.fail(error);
			finish();
		});
	}

	private async flushProtocolOperations(): Promise<void> {
		while (!this.disposed) {
			const operation = this.protocolOperations.shift();
			if (!operation) return;
			try {
				await this.processProtocolOperation(operation);
			} catch (error) {
				this.rejectProtocolOperation(operation, error);
				if (!this.controller.signal.aborted) this.fail(error);
				return;
			}
		}
	}

	private async processProtocolOperation(
		operation: ProtocolOperation,
		call?: QueuedToolCall,
		signal = this.controller.signal,
	): Promise<void> {
		const process = (name: string, args: Record<string, unknown>): Promise<number> =>
			call ? this.processProtocolCall(call, name, args, signal) : this.callAndProcess(name, args);
		if (operation.kind === "poll") {
			const count = await process("anywidget_poll", {
				instance_id: this.instanceId,
				operation_id: operation.operationId,
			});
			operation.resolve(count);
			return;
		}
		if (this.models.get(operation.model.modelId) !== operation.model) {
			if (operation.kind === "custom") {
				operation.reject?.(new Error(`Unknown anywidget model ${operation.model.modelId}`));
			}
			return;
		}
		const serialized =
			operation.kind === "update"
				? serializeUpdate(operation.state)
				: { data: operation.data, buffers: operation.buffers };
		await process("anywidget_comm", {
			instance_id: this.instanceId,
			model_id: operation.model.modelId,
			data: serialized.data,
			buffers: serialized.buffers,
			operation_id: operation.operationId,
		});
		this.noteProtocolActivity();
		if (operation.kind === "custom") operation.resolve?.();
	}

	private appendCustom(
		model: BridgeModel,
		data: CommData,
		buffers: string[],
		resolve?: () => void,
		reject?: (error: unknown) => void,
	): CustomProtocolOperation | undefined {
		if (this.disposed || this.models.get(model.modelId) !== model) return undefined;
		const operation: CustomProtocolOperation = {
			kind: "custom",
			sequence: ++this.protocolSequence,
			model,
			data,
			buffers,
			operationId: randomId(),
			resolve,
			reject,
		};
		this.protocolOperations.push(operation);
		this.startProtocolOperations();
		return operation;
	}

	private runInitializeOperations(
		scope: InitializeProtocolScope,
		operation: CustomProtocolOperation,
		signal: AbortSignal,
	): Promise<void> {
		const task = scope.tail.then(async () => {
			try {
				signal.throwIfAborted();
				await this.drainProtocolOperations(scope.call, operation.sequence, signal);
				signal.throwIfAborted();
			} catch (error) {
				scope.error ??= error;
				const index = this.protocolOperations.indexOf(operation);
				if (index >= 0) this.protocolOperations.splice(index, 1);
				operation.reject?.(error);
				throw error;
			}
		});
		scope.tail = task.then(
			() => undefined,
			() => undefined,
		);
		return task;
	}

	private async drainProtocolOperations(
		call: QueuedToolCall,
		throughSequence: number,
		signal: AbortSignal,
	): Promise<void> {
		while (!this.disposed) {
			const operation = this.protocolOperations[0];
			if (!operation || operation.sequence > throughSequence) return;
			this.protocolOperations.shift();
			try {
				await this.processProtocolOperation(operation, call, signal);
			} catch (error) {
				this.rejectProtocolOperation(operation, error);
				throw error;
			}
		}
	}

	private cancelQueuedProtocolOperations(error: unknown): void {
		for (const operation of this.protocolOperations) {
			this.rejectProtocolOperation(operation, error);
		}
		this.protocolOperations.length = 0;
	}

	private rejectProtocolOperation(operation: ProtocolOperation, error: unknown): void {
		if (operation.kind === "poll" || operation.kind === "custom") operation.reject?.(error);
	}

	private callAndProcess(name: string, args: Record<string, unknown>): Promise<number> {
		return this.calls.transaction(
			(call) => this.processProtocolCall(call, name, args),
			this.controller.signal,
		);
	}

	private async processProtocolCall(
		call: QueuedToolCall,
		name: string,
		args: Record<string, unknown>,
		signal = this.controller.signal,
	): Promise<number> {
		const previousCall = this.activeProtocolCall;
		this.activeProtocolCall = call;
		try {
			const result = await this.callWithRetry(call, name, args, signal);
			signal.throwIfAborted();
			if (result.isError) throw new Error(toolErrorText(result));
			return await this.processResult(result, call);
		} catch (error) {
			if (!this.controller.signal.aborted && !signal.aborted) this.fail(error);
			throw error;
		} finally {
			this.activeProtocolCall = previousCall;
			if (!previousCall) this.startProtocolOperations();
		}
	}

	private async callWithRetry(
		call: QueuedToolCall,
		name: string,
		args: Record<string, unknown>,
		signal = this.controller.signal,
	): Promise<CallToolResult> {
		for (let attempt = 0; ; attempt += 1) {
			try {
				signal.throwIfAborted();
				return await abortable(call(name, args), signal);
			} catch (error) {
				signal.throwIfAborted();
				const retryDelay = TRANSPORT_RETRY_DELAYS_MS[attempt];
				if (retryDelay === undefined) throw error;
				await delay(retryDelay, signal);
				signal.throwIfAborted();
			}
		}
	}

	private fail(error: unknown): void {
		reportRuntimeError(error);
		void this.dispose(error).catch(reportRuntimeError);
	}

	private processResult(result: CallToolResult, call?: QueuedToolCall): Promise<number> {
		return this.applyMessages(result, call);
	}

	private async applyMessages(result: CallToolResult, call?: QueuedToolCall): Promise<number> {
		if (this.disposed) return 0;
		const added = normalizeModelChanges(resultModels(result));
		const registered: string[] = [];
		try {
			for (const payload of added) {
				this.registerModel(payload);
				registered.push(payload.modelId);
			}

			const messages = resultMessages(result);
			for (const message of messages) {
				const modelId = message.modelId;
				if (!modelId || !message.data) continue;
				this.model(modelId).receive(message.data, decodeBuffers(message.buffers));
			}
			const context = resultContext(result);
			if (context) this.contextSync.enqueue(context);
			const contextError = resultContextError(result);
			if (contextError) console.warn(contextError);

			const removedModelIds = resultRemovedModelIds(result);
			if (removedModelIds.includes(this.rootModelId)) {
				throw new Error("The root anywidget model cannot be removed");
			}
			for (const modelId of registered) await this.binding(modelId).initialize(call);
			if (this.disposed) return 0;
			await this.removeModels(removedModelIds);
			return messages.length + added.length + removedModelIds.length + (context ? 1 : 0);
		} catch (error) {
			await this.removeModels(registered);
			throw error;
		}
	}

	private registerModel(payload: ModelPayload): void {
		if (this.models.has(payload.modelId) || this.bindings.has(payload.modelId)) {
			throw new Error(`Duplicate anywidget model ${payload.modelId}`);
		}
		const model = new BridgeModel(this, payload, showError, this.controller.signal);
		this.models.set(payload.modelId, model);
		this.bindings.set(payload.modelId, this.createBinding(this, model));
	}

	private applyLaunchMessages(value: unknown): void {
		if (value === undefined) return;
		if (!Array.isArray(value)) throw new Error("Widget launch messages must be an array");
		const messages = value.map((item, index) => {
			if (!isRecord(item)) throw new Error(`Invalid widget launch message ${index}`);
			const raw = item as RawCommMessage;
			const modelId = requiredString(raw.modelId, `model ID for launch message ${index}`);
			const model = this.models.get(modelId);
			if (!model) {
				throw new Error(`Widget launch message references unknown model ${modelId}`);
			}
			if (!isRecord(raw.data)) {
				throw new Error(`Widget launch message ${index} has no comm data`);
			}
			if (raw.buffers !== undefined && !Array.isArray(raw.buffers)) {
				throw new Error(`Widget launch message ${index} buffers must be an array`);
			}
			return { model, data: raw.data, buffers: decodeBuffers(raw.buffers) };
		});
		for (const message of messages) message.model.receive(message.data, message.buffers);
	}

	private async removeModels(modelIds: string[]): Promise<void> {
		const bindings: RuntimeBinding[] = [];
		for (const modelId of modelIds) {
			const binding = this.bindings.get(modelId);
			this.models.get(modelId)?.dispose();
			this.bindings.delete(modelId);
			this.models.delete(modelId);
			if (binding) bindings.push(binding);
		}
		await Promise.allSettled(bindings.map((binding) => this.disposeBinding(binding)));
	}

	private async disposeBinding(binding: RuntimeBinding): Promise<void> {
		await withTimeout(
			Promise.resolve().then(() => binding.dispose()),
			RUNTIME_LIFECYCLE_TIMEOUT_MS,
			"Timed out while disposing anywidget binding",
		).catch((error) => console.error(error));
	}
}

function toolErrorText(result: CallToolResult): string {
	const text = result.content.find((item) => item.type === "text");
	return text?.text ?? "AnyWidget comm failed";
}

function runtimePayload(result: CallToolResult): RawRuntimePayload | undefined {
	const meta = result._meta;
	if (!isRecord(meta)) return undefined;
	const payload = meta.anywidget;
	if (!isRecord(payload)) return undefined;
	if (!("rootModelId" in payload)) return undefined;
	return payload as unknown as RawRuntimePayload;
}

function resultMessages(result: CallToolResult): RawCommMessage[] {
	const meta = isRecord(result._meta) ? result._meta.anywidget : undefined;
	if (isRecord(meta) && Array.isArray(meta.messages)) return meta.messages as RawCommMessage[];
	return [];
}

function resultModels(result: CallToolResult): unknown {
	const meta = isRecord(result._meta) ? result._meta.anywidget : undefined;
	return isRecord(meta) ? meta.models : undefined;
}

function resultRemovedModelIds(result: CallToolResult): string[] {
	const meta = isRecord(result._meta) ? result._meta.anywidget : undefined;
	if (!isRecord(meta) || meta.removedModelIds === undefined) return [];
	if (!Array.isArray(meta.removedModelIds)) {
		throw new Error("Widget model removals must be an array");
	}
	return meta.removedModelIds.map((value) => requiredString(value, "removed model ID"));
}

function resultContext(result: CallToolResult): ModelContextSnapshot | undefined {
	const meta = isRecord(result._meta) ? result._meta.anywidget : undefined;
	if (!isRecord(meta)) return undefined;
	return normalizeContext(meta.context);
}

function resultContextError(result: CallToolResult): string | undefined {
	const meta = isRecord(result._meta) ? result._meta.anywidget : undefined;
	return isRecord(meta) && typeof meta.contextError === "string" ? meta.contextError : undefined;
}

function normalizeContext(value: unknown): ModelContextSnapshot | undefined {
	if (!isRecord(value)) return undefined;
	if (typeof value.version !== "number" || typeof value.tool !== "string" || !isRecord(value.state))
		return undefined;
	return {
		version: value.version,
		tool: value.tool,
		state: value.state,
	};
}

function normalizeModels(models: Record<string, unknown> | undefined): ModelPayload[] {
	if (!isRecord(models)) throw new Error("Widget payload has no models");

	return Object.entries(models).map(([entryId, value]) => {
		if (!isRecord(value)) throw new Error(`Invalid widget model ${entryId}`);
		const raw = value as RawModelPayload;
		const modelId = requiredString(raw.modelId ?? entryId, "model ID");
		if (!isRecord(raw.state)) throw new Error(`Model ${modelId} has no state`);
		const bufferPaths = Array.isArray(raw.bufferPaths) ? raw.bufferPaths : [];
		const state = insertBuffers({ ...raw.state }, bufferPaths, decodeBuffers(raw.buffers));
		const esm = requiredString(state._esm, `ESM for model ${modelId}`);
		const cssValue = state._css;
		const css = typeof cssValue === "string" ? cssValue : undefined;
		return { modelId, state, esm, css };
	});
}

function normalizeModelChanges(models: unknown): ModelPayload[] {
	if (models === undefined) return [];
	if (!isRecord(models)) throw new Error("Widget model changes must be an object");
	return normalizeModels(models);
}

function decodeBuffers(raw: unknown[] | undefined): DataView[] {
	return (raw ?? []).map((value) => {
		if (typeof value !== "string") throw new Error("Widget buffer must be base64 text");
		const binary = atob(value);
		const bytes = new Uint8Array(binary.length);
		for (let index = 0; index < binary.length; index += 1) {
			bytes[index] = binary.charCodeAt(index);
		}
		return new DataView(bytes.buffer);
	});
}

function parseWidgetRef(ref: string): string {
	const prefix = "anywidget:";
	if (!ref.startsWith(prefix) || ref.length === prefix.length) {
		throw new Error(`Invalid anywidget reference ${ref}`);
	}
	return ref.slice(prefix.length);
}

function requiredString(value: unknown, label: string): string {
	if (typeof value !== "string" || value.length === 0) throw new Error(`Missing ${label}`);
	return value;
}

function isRecord(value: unknown): value is Record<string, unknown> {
	return typeof value === "object" && value !== null && !Array.isArray(value);
}

function getElement<T extends HTMLElement>(id: string): T {
	const element = document.getElementById(id);
	if (!element) throw new Error(`Missing element #${id}`);
	return element as T;
}

function randomId(): string {
	return globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;
}

function abortReason(signal: AbortSignal): unknown {
	return signal.reason ?? new DOMException("Widget runtime is closed", "AbortError");
}

function abortable<T>(task: Promise<T>, signal: AbortSignal): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		let settled = false;
		const finish = (callback: () => void): void => {
			if (settled) return;
			settled = true;
			signal.removeEventListener("abort", abort);
			callback();
		};
		const abort = (): void => finish(() => reject(abortReason(signal)));

		if (signal.aborted) {
			abort();
		} else {
			signal.addEventListener("abort", abort, { once: true });
		}
		void task.then(
			(value) => finish(() => resolve(value)),
			(error: unknown) => finish(() => reject(error)),
		);
	});
}

function delay(milliseconds: number, signal: AbortSignal): Promise<void> {
	return new Promise((resolve) => {
		if (signal.aborted) {
			resolve();
			return;
		}
		let settled = false;
		const finish = (): void => {
			if (settled) return;
			settled = true;
			globalThis.clearTimeout(timeout);
			signal.removeEventListener("abort", finish);
			resolve();
		};
		const timeout = globalThis.setTimeout(finish, milliseconds);
		signal.addEventListener("abort", finish, { once: true });
	});
}

function withTimeout<T>(task: Promise<T>, milliseconds: number, message: string): Promise<T> {
	return new Promise<T>((resolve, reject) => {
		const timeout = globalThis.setTimeout(() => reject(new Error(message)), milliseconds);
		void task.then(
			(value) => {
				globalThis.clearTimeout(timeout);
				resolve(value);
			},
			(error) => {
				globalThis.clearTimeout(timeout);
				reject(error);
			},
		);
	});
}
