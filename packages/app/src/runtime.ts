import type { App } from "@modelcontextprotocol/ext-apps";
import type { CallToolResult } from "@modelcontextprotocol/sdk/types.js";

import { AssetStore, requireProtocolVersion } from "./assets";
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
	scopedModel,
	serializeCustom,
	serializeUpdate,
	type CommData,
	type ModelPayload,
} from "./model";
import {
	abortable,
	abortReason,
	disposeServerSession,
	randomId,
	RUNTIME_LIFECYCLE_TIMEOUT_MS,
	toolErrorText,
	withTimeout,
} from "./runtime-lifecycle";
import {
	decodeBuffers,
	hydrateRawMessages,
	hydrateRawModels,
	hydrateRuntimePayload,
	isRecord,
	normalizeContext,
	normalizeMessages,
	normalizeModelChanges,
	normalizeModels,
	parseWidgetRef,
	pollDelayLimit,
	type RawCommMessage,
	type RawRuntimePayload,
	requiredString,
	resultAnywidget,
	resultContext,
	resultContextError,
	resultMessages,
	resultModels,
	resultRemovedModelIds,
	sourceRefValues,
} from "./runtime-payload";
import { ToolCallQueue, type QueuedToolCall } from "./tool-calls";
import { retryTransport } from "./transport";

type BindingFactory = (runtime: WidgetRuntime, model: BridgeModel) => RuntimeBinding;
type ErrorReporter = (error: unknown) => void;
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
	signal: AbortSignal;
	dispatched: boolean;
	completed: boolean;
	abortDeadline?: ReturnType<typeof globalThis.setTimeout>;
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
	private readonly pendingRemovalAcknowledgments = new Set<string>();
	private protocolSequence = 0;
	private protocolTask?: Promise<void>;
	private activeProtocolCall?: QueuedToolCall;
	private disposeTask?: Promise<void>;
	private disposed = false;
	private pollTask?: Promise<void>;
	private pollActivityVersion = 0;
	private pollWake?: () => void;
	private readonly assetStore?: AssetStore;
	private readonly pollDelayLimitMs: number;

	static async create(
		payload: RawRuntimePayload,
		calls: ToolCallQueue,
		app: App,
		connected: Promise<void>,
		createBinding?: BindingFactory,
		signal?: AbortSignal,
		reportError: ErrorReporter = (error) => console.error(error),
	): Promise<WidgetRuntime> {
		const instanceId = requiredString(payload.instanceId, "instance ID");
		try {
			requireProtocolVersion(payload.protocolVersion);
			signal?.throwIfAborted();
			const assetStore = new AssetStore(instanceId);
			const assets = await assetStore.resolve(
				payload.assetManifest,
				sourceRefValues(payload.models, payload.messages),
				(name, args) => calls.call(name, args, signal),
				signal,
			);
			signal?.throwIfAborted();
			return new WidgetRuntime(
				hydrateRuntimePayload(payload, assets),
				calls,
				app,
				connected,
				createBinding,
				assetStore,
				reportError,
			);
		} catch (error) {
			await disposeServerSession(
				calls,
				instanceId,
				"Timed out while disposing failed widget creation",
				RUNTIME_LIFECYCLE_TIMEOUT_MS,
			).catch(() => undefined);
			throw error;
		}
	}

	constructor(
		payload: RawRuntimePayload,
		private readonly calls: ToolCallQueue,
		app: App,
		connected: Promise<void>,
		createBinding?: BindingFactory,
		assetStore?: AssetStore,
		private readonly reportError: ErrorReporter = (error) => console.error(error),
	) {
		this.instanceId = requiredString(payload.instanceId, "instance ID");
		this.rootModelId = requiredString(payload.rootModelId, "root model ID");
		this.pollDelayLimitMs = pollDelayLimit(
			payload.sessionIdleTimeoutMs,
			POLL_IDLE_DELAYS_MS.at(-1)!,
		);
		this.createBinding =
			createBinding ??
			((runtime, model) => new WidgetBinding(runtime, model, { reportError: this.reportError }));
		this.assetStore = assetStore;

		for (const modelPayload of normalizeModels(payload.models)) {
			this.registerModel(modelPayload);
		}
		if (!this.models.has(this.rootModelId)) {
			throw new Error(`Root model ${this.rootModelId} is missing from the widget payload`);
		}
		// Apply launch messages before binding initialization so lifecycle hooks see
		// authoritative state and receive queued custom messages.
		this.applyLaunchMessages(payload.messages);
		this.contextSync = new ModelContextSync(app, connected, (error) =>
			console.warn("Failed to update model context", error),
		);
		this.initialContext = normalizeContext(payload.context);
	}

	async mount(element: HTMLElement, signal = this.controller.signal): Promise<void> {
		// Start polling before binding initialization so a slow initializer cannot let
		// the server session expire.
		this.pollTask ??= this.poll();
		const initialized = await abortable(
			Promise.allSettled(Array.from(this.bindings.values(), (binding) => binding.initialize())),
			signal,
		);
		const failed = initialized.find(
			(result): result is PromiseRejectedResult => result.status === "rejected",
		);
		if (failed) throw failed.reason;
		signal.throwIfAborted();
		this.controller.signal.throwIfAborted();
		await abortable(this.binding(this.rootModelId).render(element, signal), signal);
		signal.throwIfAborted();
		this.controller.signal.throwIfAborted();
		if (this.initialContext) this.contextSync.enqueue(this.initialContext);
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
		const disposeRequest = disposeServerSession(
			this.calls,
			this.instanceId,
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

		const bindingDisposals = await Promise.allSettled(
			Array.from(this.bindings.values(), (binding) => this.disposeBinding(binding)),
		);
		for (const disposal of bindingDisposals) {
			if (disposal.status === "rejected") console.error(disposal.reason);
		}
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
					let operation: CustomProtocolOperation | undefined;
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
					const abort = () => {
						const error = abortReason(signal);
						// Remove commands that have not reached the server. Once dispatched, keep the
						// transaction alive to apply authoritative state, then fail if it stalls.
						if (operation) {
							const index = this.protocolOperations.indexOf(operation);
							if (index >= 0) {
								this.protocolOperations.splice(index, 1);
								this.rejectProtocolOperation(operation, error);
							} else if (operation.dispatched) {
								this.scheduleCancelledCommandDeadline(operation);
							}
						}
						finish(() => reject(error));
					};

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
						operation = this.appendCustom(
							model,
							serialized.data,
							serialized.buffers,
							complete,
							fail,
							signal,
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
					Math.min(POLL_IDLE_DELAYS_MS[delayIndex] ?? POLL_ACTIVE_DELAY_MS, this.pollDelayLimitMs),
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
				if (operation.kind === "custom" && operation.signal.aborted && !operation.dispatched) {
					continue;
				}
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
		signal.throwIfAborted();
		if (operation.kind === "custom") operation.signal.throwIfAborted();
		const beforeDispatch =
			operation.kind === "custom"
				? () => {
						if (operation.dispatched) return;
						operation.signal.throwIfAborted();
						operation.dispatched = true;
					}
				: undefined;
		const suppressFailure =
			operation.kind === "custom"
				? () => operation.signal.aborted && !operation.dispatched
				: undefined;
		const process = (name: string, args: Record<string, unknown>): Promise<number> =>
			call
				? this.processProtocolCall(call, name, args, signal, beforeDispatch, suppressFailure)
				: this.callAndProcess(name, args, signal, beforeDispatch, suppressFailure);
		if (operation.kind === "poll") {
			const acknowledgedModelIds = Array.from(this.pendingRemovalAcknowledgments);
			for (const modelId of acknowledgedModelIds) {
				this.pendingRemovalAcknowledgments.delete(modelId);
			}
			try {
				const count = await process("anywidget_poll", {
					instance_id: this.instanceId,
					operation_id: operation.operationId,
					acknowledged_model_ids: acknowledgedModelIds,
				});
				operation.resolve(count);
			} catch (error) {
				for (const modelId of acknowledgedModelIds) {
					this.pendingRemovalAcknowledgments.add(modelId);
				}
				throw error;
			}
			return;
		}
		if (this.models.get(operation.model.modelId) !== operation.model) {
			if (operation.kind === "custom") {
				this.rejectProtocolOperation(
					operation,
					new Error(`Unknown anywidget model ${operation.model.modelId}`),
				);
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
		if (operation.kind === "custom") {
			this.completeCustomOperation(operation);
			operation.resolve?.();
		}
	}

	private appendCustom(
		model: BridgeModel,
		data: CommData,
		buffers: string[],
		resolve?: () => void,
		reject?: (error: unknown) => void,
		signal = this.controller.signal,
	): CustomProtocolOperation | undefined {
		if (this.disposed || this.models.get(model.modelId) !== model) return undefined;
		const operation: CustomProtocolOperation = {
			kind: "custom",
			sequence: ++this.protocolSequence,
			model,
			data,
			buffers,
			operationId: randomId(),
			signal,
			dispatched: false,
			completed: false,
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
		// Drain initializer commands through the tool call whose result created the
		// model. Waiting for another global queue slot would deadlock result application.
		const task = scope.tail.then(async () => {
			try {
				signal.throwIfAborted();
				await this.drainProtocolOperations(scope.call, operation.sequence, signal);
				signal.throwIfAborted();
			} catch (error) {
				scope.error ??= error;
				const index = this.protocolOperations.indexOf(operation);
				if (index >= 0) this.protocolOperations.splice(index, 1);
				this.rejectProtocolOperation(operation, error);
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
		if (operation.kind === "custom") this.completeCustomOperation(operation);
		if (operation.kind === "poll" || operation.kind === "custom") operation.reject?.(error);
	}

	private completeCustomOperation(operation: CustomProtocolOperation): void {
		operation.completed = true;
		if (operation.abortDeadline !== undefined) {
			globalThis.clearTimeout(operation.abortDeadline);
			operation.abortDeadline = undefined;
		}
	}

	private scheduleCancelledCommandDeadline(operation: CustomProtocolOperation): void {
		if (this.disposed || operation.completed || operation.abortDeadline !== undefined) return;
		operation.abortDeadline = globalThis.setTimeout(() => {
			if (operation.completed || this.disposed) return;
			this.fail(new Error("Timed out while waiting for a cancelled widget command"));
		}, RUNTIME_LIFECYCLE_TIMEOUT_MS);
	}

	private callAndProcess(
		name: string,
		args: Record<string, unknown>,
		signal = this.controller.signal,
		beforeDispatch?: () => void,
		suppressFailure?: () => boolean,
	): Promise<number> {
		return this.calls.transaction(
			(call) => this.processProtocolCall(call, name, args, signal, beforeDispatch, suppressFailure),
			signal,
		);
	}

	private async processProtocolCall(
		call: QueuedToolCall,
		name: string,
		args: Record<string, unknown>,
		signal = this.controller.signal,
		beforeDispatch?: () => void,
		suppressFailure?: () => boolean,
	): Promise<number> {
		const previousCall = this.activeProtocolCall;
		this.activeProtocolCall = call;
		try {
			const result = await this.callWithRetry(call, name, args, signal, beforeDispatch);
			signal.throwIfAborted();
			if (result.isError) throw new Error(toolErrorText(result));
			return await this.processResult(result, call, signal);
		} catch (error) {
			if (!suppressFailure?.() && !this.controller.signal.aborted && !signal.aborted) {
				this.fail(error);
			}
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
		beforeDispatch?: () => void,
	): Promise<CallToolResult> {
		// Keep the same arguments and operation ID across transport attempts so the
		// server replays one logical operation.
		return retryTransport(() => {
			signal.throwIfAborted();
			beforeDispatch?.();
			return call(name, args, signal);
		}, signal);
	}

	private fail(error: unknown): void {
		this.reportError(error);
		void this.dispose(error).catch(this.reportError);
	}

	private processResult(
		result: CallToolResult,
		call?: QueuedToolCall,
		signal = this.controller.signal,
	): Promise<number> {
		return this.applyMessages(result, call, signal);
	}

	private async applyMessages(
		result: CallToolResult,
		call?: QueuedToolCall,
		signal = this.controller.signal,
	): Promise<number> {
		if (this.disposed) return 0;
		let rawModels = resultModels(result);
		let rawMessages: unknown = resultMessages(result);
		if (this.assetStore) {
			const payload = resultAnywidget(result);
			requireProtocolVersion(payload?.protocolVersion);
			const assets = await this.assetStore.resolve(
				payload?.assetManifest,
				sourceRefValues(rawModels, rawMessages),
				call ?? ((name, args) => this.calls.call(name, args, signal)),
				signal,
			);
			signal.throwIfAborted();
			rawModels = hydrateRawModels(rawModels, assets);
			rawMessages = hydrateRawMessages(rawMessages, assets);
		}
		// Apply graph changes as one browser transaction: register additions, deliver
		// references, initialize additions, dispose removals, then queue acknowledgments.
		const added = normalizeModelChanges(rawModels);
		const registered: string[] = [];
		try {
			for (const payload of added) {
				this.registerModel(payload);
				registered.push(payload.modelId);
			}

			const messages = normalizeMessages(rawMessages);
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
			for (const modelId of removedModelIds) {
				this.pendingRemovalAcknowledgments.add(modelId);
			}
			return messages.length + added.length + removedModelIds.length + (context ? 1 : 0);
		} catch (error) {
			try {
				await this.removeModels(registered);
			} catch (cleanupError) {
				throw new AggregateError(
					[error, cleanupError],
					"Failed to apply and roll back widget model changes",
				);
			}
			throw error;
		}
	}

	private registerModel(payload: ModelPayload): void {
		if (this.models.has(payload.modelId) || this.bindings.has(payload.modelId)) {
			throw new Error(`Duplicate anywidget model ${payload.modelId}`);
		}
		const model = new BridgeModel(this, payload, this.reportError, this.controller.signal);
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
		const disposals = await Promise.allSettled(
			bindings.map((binding) => this.disposeBinding(binding)),
		);
		const errors = disposals.flatMap((result) =>
			result.status === "rejected" ? [result.reason] : [],
		);
		if (errors.length === 1) throw errors[0];
		if (errors.length > 1) {
			throw new AggregateError(errors, "Failed to dispose removed anywidget bindings");
		}
	}

	private async disposeBinding(binding: RuntimeBinding): Promise<void> {
		await withTimeout(
			Promise.resolve().then(() => binding.dispose()),
			RUNTIME_LIFECYCLE_TIMEOUT_MS,
			"Timed out while disposing anywidget binding",
		);
	}
}

export { disposeServerSession };
