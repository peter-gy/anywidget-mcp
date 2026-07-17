import { parse, type ExportSpecifier, type ImportSpecifier } from "es-module-lexer/js";

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
	if (isRemoteModule(source)) {
		const module = (await import(/* @vite-ignore */ source)) as WidgetModule;
		signal?.throwIfAborted();
		return module;
	}
	const [imports, exports] = parse(source);
	if (requiresNamespaceImport(source, imports, exports)) {
		return loadBlobModule(source, signal);
	}
	return loadInlineModule(source, signal);
}

export function instrumentInlineModule(source: string, receiverName: string): InlineModule {
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

async function loadBlobModule(source: string, signal?: AbortSignal): Promise<WidgetModule> {
	const url = URL.createObjectURL(new Blob([source], { type: "text/javascript" }));
	try {
		const module = (await import(/* @vite-ignore */ url)) as WidgetModule;
		signal?.throwIfAborted();
		return module;
	} finally {
		URL.revokeObjectURL(url);
	}
}

async function loadInlineModule(source: string, signal?: AbortSignal): Promise<WidgetModule> {
	const receiverName = createReceiverName();
	const module = instrumentInlineModule(source, receiverName);
	const script = document.createElement("script");
	script.type = "module";
	script.textContent = module.code;
	const receivers = globalThis as unknown as Record<string, unknown>;

	return new Promise<WidgetModule>((resolve, reject) => {
		let settled = false;
		const cleanup = (): void => {
			signal?.removeEventListener("abort", abort);
			window.removeEventListener("error", runtimeError);
			script.removeEventListener("error", loadError);
			script.removeEventListener("load", loaded);
			script.remove();
			Reflect.deleteProperty(receivers, receiverName);
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
		const loaded = (): void =>
			finish(() => reject(new Error("anywidget module did not expose its exports")));
		const runtimeError = (event: ErrorEvent): void => {
			if (!matchesModuleError(event, module.sourceName)) return;
			event.preventDefault();
			finish(() => reject(moduleError(event)));
		};
		const receive = (value: WidgetModule): void => finish(() => resolve(value));

		Object.defineProperty(receivers, receiverName, {
			configurable: true,
			value: receive,
		});
		signal?.addEventListener("abort", abort, { once: true });
		window.addEventListener("error", runtimeError);
		script.addEventListener("error", loadError, { once: true });
		script.addEventListener("load", loaded, { once: true });
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

function matchesModuleError(event: ErrorEvent, sourceName: string): boolean {
	if (event.filename.includes(sourceName)) return true;
	return event.error instanceof Error && event.error.stack?.includes(sourceName) === true;
}

function moduleError(event: ErrorEvent): Error {
	if (event.error instanceof Error) return event.error;
	return new Error(event.message || "Failed to execute anywidget module");
}
