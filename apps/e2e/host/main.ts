import {
	AppBridge,
	getToolUiResourceUri,
	PostMessageTransport,
} from "@modelcontextprotocol/ext-apps/app-bridge";
import { McpUiResourceCspSchema, McpUiResourceMetaSchema } from "@modelcontextprotocol/ext-apps";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { CallToolRequestSchema, CallToolResultSchema } from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";

const tool = document.querySelector<HTMLSelectElement>("#tool")!;
const argumentsInput = document.querySelector<HTMLTextAreaElement>("#arguments")!;
const open = document.querySelector<HTMLButtonElement>("#open")!;
const read = document.querySelector<HTMLButtonElement>("#read")!;
const close = document.querySelector<HTMLButtonElement>("#close")!;
const status = document.querySelector<HTMLOutputElement>("#status")!;
const context = document.querySelector<HTMLOutputElement>("#context")!;
const state = document.querySelector<HTMLOutputElement>("#state")!;
const assets = document.querySelector<HTMLOutputElement>("#assets")!;
const requestBytes = document.querySelector<HTMLOutputElement>("#request-bytes")!;
const resultBytes = document.querySelector<HTMLOutputElement>("#result-bytes")!;
const rawBytes = document.querySelector<HTMLOutputElement>("#raw-bytes")!;
const lostReads = document.querySelector<HTMLOutputElement>("#lost-reads")!;
const disposed = document.querySelector<HTMLOutputElement>("#disposed")!;
const view = document.querySelector<HTMLDivElement>("#view")!;
const client = new Client({ name: "anywidget-e2e", version: "1.0.0" });
const resources = new Map<string, string>();
let bridge: AppBridge | undefined;
let frameUrl: string | undefined;
let stateArguments: NonNullable<Parameters<Client["callTool"]>[0]["arguments"]> = {};
let assetRequests = 0;
let disposedSessions = 0;
const toolByteLimit = 128 * 1024;
let readDropped = false;
const readArgumentsSchema = z.object({ offset: z.number().int().nonnegative() });
const readMetadataSchema = z.object({
	anywidget: z.object({ byteLength: z.number().int().nonnegative() }),
});
const resourceMetadataSchema = McpUiResourceMetaSchema.extend({
	csp: McpUiResourceCspSchema.extend({
		scriptDirectives: z.array(z.enum(["'wasm-unsafe-eval'", "'unsafe-eval'"])).optional(),
	}).optional(),
});

async function callTool(params: Parameters<Client["callTool"]>[0]) {
	const requestSize = new TextEncoder().encode(JSON.stringify(params)).byteLength;
	requestBytes.value = String(Math.max(Number(requestBytes.value), requestSize));
	if (requestSize > toolByteLimit) {
		throw new Error(`Tool ${params.name} request exceeded ${toolByteLimit} bytes: ${requestSize}`);
	}
	const result = await client.callTool(params);
	const resultSize = new TextEncoder().encode(JSON.stringify(result)).byteLength;
	resultBytes.value = String(Math.max(Number(resultBytes.value), resultSize));
	if (resultSize > toolByteLimit) {
		throw new Error(`Tool ${params.name} result exceeded ${toolByteLimit} bytes: ${resultSize}`);
	}
	if (params.name === "anywidget_read" && !result.isError) {
		const { offset } = readArgumentsSchema.parse(params.arguments);
		const { anywidget } = readMetadataSchema.parse(result._meta);
		rawBytes.value = String(Math.max(Number(rawBytes.value), anywidget.byteLength));
		if (new URLSearchParams(location.search).has("drop-read") && !readDropped && offset >= 65536) {
			readDropped = true;
			lostReads.value = "1";
			throw new Error("Read response lost after the server completed it");
		}
	}
	return CallToolResultSchema.parse(result);
}

function report(error: Error): void {
	open.disabled = false;
	status.value = error.message;
}

async function closeWidget(): Promise<void> {
	close.disabled = true;
	status.value = "Closing";
	const closing = bridge;
	bridge = undefined;
	if (closing) {
		await closing.teardownResource({});
		await closing.close();
	}
	view.replaceChildren();
	if (frameUrl) URL.revokeObjectURL(frameUrl);
	frameUrl = undefined;
	close.disabled = true;
	status.value = "Closed";
}

async function openWidget(): Promise<void> {
	open.disabled = true;
	await closeWidget();
	context.value = "";
	state.value = "";
	status.value = "Opening";
	const request = CallToolRequestSchema.parse({
		method: "tools/call",
		params: { name: tool.value, arguments: JSON.parse(argumentsInput.value) },
	});
	const result = await callTool(request.params);
	if (result.isError) throw new Error(JSON.stringify(result.content));
	stateArguments = { state_id: result.structuredContent?.state_id };
	read.disabled = stateArguments.state_id == null;
	const uri = resources.get(tool.value);
	if (!uri) throw new Error("The tool has no app resource");
	const resource = await client.readResource({ uri });
	const html = resource.contents[0];
	if (!html || !("text" in html)) throw new Error("The app resource must contain HTML");
	const metadata = resourceMetadataSchema.parse(html._meta?.ui ?? {});
	const resourceDomains = metadata.csp?.resourceDomains?.join(" ") || "'none'";
	const document = new DOMParser().parseFromString(html.text, "text/html");
	const policy = document.createElement("meta");
	policy.httpEquiv = "Content-Security-Policy";
	policy.content = [
		"default-src 'none'",
		`script-src 'unsafe-inline' ${metadata.csp?.scriptDirectives?.join(" ") ?? ""} ${resourceDomains}`,
		`style-src 'unsafe-inline' ${resourceDomains}`,
		`img-src data: ${resourceDomains}`,
		`font-src ${resourceDomains}`,
		`worker-src ${resourceDomains}`,
		`connect-src ${metadata.csp?.connectDomains?.join(" ") || "'none'"}`,
	].join("; ");
	document.head.prepend(policy);
	const iframe = window.document.createElement("iframe");
	iframe.title = "Widget";
	iframe.sandbox.add("allow-scripts", "allow-same-origin");
	view.append(iframe);
	if (!iframe.contentWindow) throw new Error("The widget frame has no window");
	const pullOnly = new URLSearchParams(location.search).has("pull-only");
	bridge = new AppBridge(
		null,
		{ name: "anywidget-e2e", version: "1.0.0" },
		{
			serverTools: {},
			updateModelContext: pullOnly ? undefined : { structuredContent: {} },
		},
		{ hostContext: { theme: "light", displayMode: "inline" } },
	);
	bridge.onupdatemodelcontext = async (params) => {
		context.value = JSON.stringify(params.structuredContent);
		return {};
	};
	bridge.onsizechange = ({ height }) => {
		if (height) iframe.style.height = `${height}px`;
	};
	const forward: NonNullable<AppBridge["oncalltool"]> = async (params) => {
		if (params.name === "anywidget_read") assets.value = String(++assetRequests);
		const response = await callTool(params);
		if (params.name === "anywidget_dispose" && !response.isError) {
			disposed.value = String(++disposedSessions);
		}
		return response;
	};
	const activeBridge = bridge;
	bridge.oninitialized = () => {
		void activeBridge
			.sendToolInput({ arguments: request.params.arguments ?? {} })
			.then(() => activeBridge.sendToolResult(result))
			.then(() => {
				status.value = "Connected";
				open.disabled = false;
				close.disabled = false;
			})
			.catch(report);
	};
	await bridge.connect(new PostMessageTransport(iframe.contentWindow, iframe.contentWindow));
	bridge.oncalltool = forward;
	// Blob URLs keep the host origin stable across app documents for CacheStorage.
	frameUrl = URL.createObjectURL(
		new Blob([document.documentElement.outerHTML], { type: "text/html" }),
	);
	iframe.src = frameUrl;
}

open.addEventListener("click", () => {
	void openWidget().catch(report);
});
close.addEventListener("click", () => {
	void closeWidget().catch(report);
});
read.addEventListener("click", () => {
	void callTool({ name: "anywidget_state", arguments: stateArguments })
		.then((result) => {
			state.value = JSON.stringify(result);
		})
		.catch(report);
});

await client.connect(new StreamableHTTPClientTransport(new URL("/mcp", location.href)));
const listing = await client.listTools();
for (const entry of listing.tools) {
	const uri = getToolUiResourceUri(entry);
	if (uri) {
		resources.set(entry.name, uri);
		tool.add(new Option(entry.name, entry.name));
	}
}
status.value = "Ready";
open.disabled = false;
