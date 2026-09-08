import { abortable, withTimeout } from "../abort";
import type { RuntimeRecord } from "../runtime-value";

export interface WebMCPTool {
	name: string;
	title: string;
	description: string;
	inputSchema: object;
	annotations: { readOnlyHint: boolean };
	execute(input: RuntimeRecord, options: { signal?: AbortSignal }): Promise<RuntimeRecord>;
}
interface ModelContext {
	registerTool(tool: WebMCPTool, options: { signal: AbortSignal }): Promise<void>;
}
const registryKey = Symbol.for("anywidget-mcp.webmcp.registry.v1");
export interface ToolDocument extends Document {
	modelContext?: ModelContext;
	permissionsPolicy?: { allowsFeature(name: string): boolean };
	[registryKey]?: Map<string, Registration>;
}
type BuildTools = (signal: AbortSignal) => WebMCPTool[];
interface Owner {
	fingerprint: string;
	build: BuildTools;
}
interface Registration {
	owners: Set<Owner>;
	active: Owner;
	fingerprint: string;
	controller: AbortController;
	ready: Promise<void>;
}
export interface ToolLease {
	release(): void;
	ready(): Promise<void>;
	update(fingerprint: string, build: BuildTools): void;
}

export function acquireTools(
	target: ToolDocument,
	key: string,
	fingerprint: string,
	build: BuildTools,
): ToolLease {
	// Bundled widget modules share this document-owned registry across module copies.
	const registry = (target[registryKey] ??= new Map());
	const owner = { fingerprint, build };
	let registration = registry.get(key);
	if (!registration) {
		registration = {
			owners: new Set(),
			active: owner,
			fingerprint: owner.fingerprint,
			controller: new AbortController(),
			ready: Promise.resolve(),
		};
		registry.set(key, registration);
		register(target, registration);
	} else if (registration.controller.signal.aborted) {
		registration.active = owner;
		register(target, registration);
	}
	const entry = registration;
	entry.owners.add(owner);
	const lifetime = new AbortController();
	return {
		async ready() {
			for (;;) {
				lifetime.signal.throwIfAborted();
				const pending = entry.ready;
				try {
					// eslint-disable-next-line no-await-in-loop -- Follow each replacement after the preceding registration settles.
					await abortable(pending, lifetime.signal);
				} catch (error) {
					if (lifetime.signal.aborted || pending === entry.ready) throw error;
				}
				if (pending === entry.ready) return;
			}
		},
		update(next, nextBuild) {
			if (lifetime.signal.aborted) return;
			owner.fingerprint = next;
			owner.build = nextBuild;
			if (owner.fingerprint === entry.fingerprint && !entry.controller.signal.aborted) return;
			entry.active = owner;
			register(target, entry);
		},
		release() {
			if (lifetime.signal.aborted) return;
			lifetime.abort();
			entry.owners.delete(owner);
			if (!entry.owners.size) {
				entry.controller.abort();
				registry.delete(key);
				if (!registry.size) delete target[registryKey];
			} else if (entry.active === owner) {
				const next = entry.owners.values().next().value;
				if (next) {
					entry.active = next;
					register(target, entry);
				}
			}
		},
	};
}

function register(target: ToolDocument, entry: Registration): void {
	entry.controller.abort();
	const controller = new AbortController();
	entry.controller = controller;
	entry.fingerprint = entry.active.fingerprint;
	const tools = entry.active.build(controller.signal);
	const pending = Promise.all(
		tools.map((tool) =>
			Promise.resolve().then(() => {
				controller.signal.throwIfAborted();
				return target.modelContext?.registerTool(tool, { signal: controller.signal });
			}),
		),
	).then(() => undefined);
	entry.ready = withTimeout(
		abortable(pending, controller.signal),
		10_000,
		"Timed out registering AnyWidget WebMCP tools.",
	);
	void entry.ready.catch((error) => {
		if (controller.signal.aborted) return;
		controller.abort();
		console.warn("Could not register AnyWidget WebMCP tools.", error);
	});
}

export function toolDocument(document: Document): ToolDocument | undefined {
	let current: ToolDocument = document;
	if (current.permissionsPolicy?.allowsFeature("tools") === false) return undefined;
	let target = current.modelContext ? current : undefined;
	while (current.defaultView && current.defaultView.parent !== current.defaultView) {
		try {
			current = current.defaultView.parent.document;
		} catch {
			break;
		}
		if (current.permissionsPolicy?.allowsFeature("tools") === false) break;
		if (current.modelContext) target = current;
	}
	return target;
}
