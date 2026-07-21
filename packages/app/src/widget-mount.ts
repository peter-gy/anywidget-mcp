export async function mountDetachedWidget(
	host: HTMLElement,
	render: (element: HTMLElement) => Promise<void>,
	signal?: AbortSignal,
): Promise<void> {
	// AnyWidget hosts render a view before attaching view.el. Keep the target
	// detached so renderers can use el.getRootNode() as a local style container.
	const element = host.ownerDocument.createElement("div");
	await render(element);
	signal?.throwIfAborted();
	host.replaceChildren(element);
}
