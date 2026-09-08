import { parse, type ExportSpecifier, type ImportSpecifier } from "es-module-lexer/js";
import { abortable, withTimeout } from "./abort";
import { isPlainObject } from "./runtime-value";

export interface WidgetModule {
	default?: unknown;
	render?: unknown;
}

interface InlineModule {
	code: string;
	sourceName: string;
}

let nextModuleId = 0;

export async function loadModule(source: string, signal?: AbortSignal): Promise<WidgetModule> {
	signal?.throwIfAborted();
	const controller = new AbortController();
	const lifetime = signal ? AbortSignal.any([signal, controller.signal]) : controller.signal;
	try {
		let loading: Promise<WidgetModule>;
		if (isRemoteModule(source)) {
			loading = import(/* @vite-ignore */ source).then(normalizeWidgetModule);
		} else {
			const [imports, exports] = parse(source);
			// Script elements avoid dynamic-import failures in embedded hosts.
			// Namespace re-exports require a module namespace and use dynamic import.
			loading = requiresNamespaceImport(source, imports, exports)
				? loadBlobModule(source, lifetime)
				: loadScriptModule(source, lifetime);
		}
		return await withTimeout(
			abortable(loading, lifetime),
			10_000,
			"Timed out loading anywidget module",
		);
	} finally {
		controller.abort();
	}
}

export function instrumentInlineModule(source: string, receiverName: string): InlineModule {
	// Inline module scripts do not expose their namespace to the creator. Rewrite
	// supported exports and publish them through a one-shot receiver.
	const [, exports] = parse(source);
	const defaultExport = findExport(exports, "default");
	const renderExport = findExport(exports, "render");
	const defaultBinding = `${receiverName}_default`;
	let code = source;
	let defaultReference: string | undefined;

	if (defaultExport) {
		if (defaultExport.ln) {
			defaultReference = defaultExport.ln;
		} else if (isDirectDefault(source, defaultExport)) {
			code = captureDefaultExport(source, defaultExport, defaultBinding);
			defaultReference = defaultBinding;
		} else defaultReference = localExportName(defaultExport, "default");
	}

	const renderReference = renderExport ? localExportName(renderExport, "render") : undefined;
	const fields = [
		defaultReference ? `default: ${defaultReference}` : undefined,
		renderReference ? `render: ${renderReference}` : undefined,
	].filter((field): field is string => field !== undefined);
	const sourceName = `${receiverName}.mjs`;
	code += `\n;globalThis[${JSON.stringify(receiverName)}]?.({ ${fields.join(", ")} });`;
	code += `\n//# sourceURL=${sourceName}\n`;
	return { code, sourceName };
}

async function loadBlobModule(source: string, signal: AbortSignal): Promise<WidgetModule> {
	const url = URL.createObjectURL(new Blob([source], { type: "text/javascript" }));
	try {
		const module = normalizeWidgetModule(await abortable(import(/* @vite-ignore */ url), signal));
		signal.throwIfAborted();
		return module;
	} finally {
		URL.revokeObjectURL(url);
	}
}

async function loadScriptModule(source: string, signal?: AbortSignal): Promise<WidgetModule> {
	const receiverName = createReceiverName();
	const module = instrumentInlineModule(source, receiverName);
	const script = document.createElement("script");
	const url = URL.createObjectURL(new Blob([module.code], { type: "text/javascript" }));
	script.type = "module";
	// WebKit reports the script URL for evaluation errors and ignores sourceURL
	// labels on inline modules. Each load needs its own error identity.
	script.src = url;

	return new Promise<WidgetModule>((resolve, reject) => {
		let settled = false;
		const cleanup = (): void => {
			signal?.removeEventListener("abort", abort);
			window.removeEventListener("error", runtimeError);
			script.removeEventListener("error", loadError);
			script.remove();
			URL.revokeObjectURL(url);
			Reflect.deleteProperty(globalThis, receiverName);
		};
		const finish = (callback: () => void): void => {
			if (settled) return;
			settled = true;
			cleanup();
			callback();
		};
		const abort = (): void =>
			finish(() =>
				reject(signal?.reason ?? new DOMException("Widget module load cancelled", "AbortError")),
			);
		const loadError = (event: Event): void => {
			event.preventDefault();
			finish(() => reject(new Error("Failed to execute anywidget module")));
		};
		const runtimeError = (event: ErrorEvent): void => {
			if (!matchesModuleError(event, url, module.sourceName)) return;
			event.preventDefault();
			finish(() => reject(moduleError(event)));
		};
		const receive = (value: WidgetModule): void => finish(() => resolve(value));

		Object.defineProperty(globalThis, receiverName, {
			configurable: true,
			value: receive,
		});
		signal?.addEventListener("abort", abort, { once: true });
		window.addEventListener("error", runtimeError);
		script.addEventListener("error", loadError, { once: true });
		if (signal?.aborted) {
			abort();
			return;
		}
		try {
			document.head.append(script);
		} catch (error) {
			finish(() => reject(error));
		}
	});
}

function normalizeWidgetModule<Value>(value: Value): WidgetModule {
	if (!isPlainObject(value)) throw new Error("anywidget module namespace must be an object");
	return {
		default: Object.getOwnPropertyDescriptor(value, "default")?.value,
		render: Object.getOwnPropertyDescriptor(value, "render")?.value,
	};
}

function findExport(
	exports: ReadonlyArray<ExportSpecifier>,
	name: "default" | "render",
): ExportSpecifier | undefined {
	return exports.find((entry) => entry.n === name);
}

function isDirectDefault(source: string, entry: ExportSpecifier): boolean {
	const prefix = source.slice(entry.ss, entry.s);
	if (!prefix.startsWith("export")) return false;
	const trivia = prefix
		.slice("export".length)
		.replaceAll(/\/\*[\s\S]*?\*\/|\/\/[^\r\n]*/g, "")
		.trim();
	return trivia.length === 0;
}

function captureDefaultExport(source: string, entry: ExportSpecifier, binding: string): string {
	const declarationName = anonymousDeclarationNameOffset(source, entry.e);
	if (declarationName !== undefined) {
		return (
			source.slice(0, entry.ss) +
			source.slice(entry.e, declarationName) +
			` ${binding}` +
			source.slice(declarationName)
		);
	}
	return source.slice(0, entry.ss) + `const ${binding} =` + source.slice(entry.e);
}

function anonymousDeclarationNameOffset(source: string, start: number): number | undefined {
	let cursor = skipTrivia(source, start);
	if (keywordAt(source, cursor, "async")) cursor = skipTrivia(source, cursor + "async".length);
	if (keywordAt(source, cursor, "class")) return cursor + "class".length;
	if (!keywordAt(source, cursor, "function")) return undefined;

	const afterFunction = cursor + "function".length;
	const star = skipTrivia(source, afterFunction);
	return source[star] === "*" ? star + 1 : afterFunction;
}

function skipTrivia(source: string, start: number): number {
	let cursor = start;
	while (cursor < source.length) {
		if (/\s/.test(source[cursor] ?? "")) {
			cursor += 1;
			continue;
		}
		if (source.startsWith("//", cursor)) {
			const newline = source.indexOf("\n", cursor + 2);
			if (newline === -1) return source.length;
			cursor = newline + 1;
			continue;
		}
		if (source.startsWith("/*", cursor)) {
			const close = source.indexOf("*/", cursor + 2);
			if (close === -1) return source.length;
			cursor = close + 2;
			continue;
		}
		break;
	}
	return cursor;
}

function keywordAt(source: string, start: number, keyword: string): boolean {
	if (!source.startsWith(keyword, start)) return false;
	return !/[\p{ID_Continue}$\u200c\u200d]/u.test(source[start + keyword.length] ?? "");
}

function requiresNamespaceImport(
	source: string,
	imports: ReadonlyArray<ImportSpecifier>,
	exports: ReadonlyArray<ExportSpecifier>,
): boolean {
	if (imports.some((entry) => isStarReexport(source, entry))) return true;
	return exports.some(
		(entry) =>
			(entry.n === "default" || entry.n === "render") &&
			entry.ln === undefined &&
			imports.some((importEntry) => importEntry.ss === entry.ss),
	);
}

function isStarReexport(source: string, entry: ImportSpecifier): boolean {
	const prefix = source.slice(entry.ss, entry.s).replaceAll(/\/\*[\s\S]*?\*\/|\/\/[^\r\n]*/g, "");
	return /^\s*export\s*\*/.test(prefix);
}

function localExportName(entry: ExportSpecifier, name: string): string {
	if (entry.ln) return entry.ln;
	throw new Error(`anywidget ${name} export must reference a local binding`);
}

function createReceiverName(): string {
	const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "") ?? "";
	nextModuleId += 1;
	return `__anywidget_mcp_module_${random}${nextModuleId.toString(36)}`;
}

function isRemoteModule(value: string): boolean {
	return value.startsWith("http://") || value.startsWith("https://");
}

function matchesModuleError(event: ErrorEvent, url: string, sourceName: string): boolean {
	return [url, sourceName].some(
		(source) =>
			event.filename.includes(source) ||
			(event.error instanceof Error && event.error.stack?.includes(source) === true),
	);
}

function moduleError(event: ErrorEvent): Error {
	if (event.error instanceof Error) return event.error;
	return new Error(event.message || "Failed to execute anywidget module");
}
