import type { AnyModel } from "../model";
import { isCallable, isString, type WidgetValue } from "../runtime-value";

type HostSlot = (sender: WidgetValue, value: WidgetValue) => void;
interface HostSignal {
	connect(slot: HostSlot): boolean;
	disconnect(slot: HostSlot): boolean;
}

export function connectionSignal(model: AnyModel, lifetime: AbortSignal): AbortSignal {
	const controller = new AbortController();
	const signal = AbortSignal.any([lifetime, controller.signal]);
	if (signal.aborted) return signal;
	const disconnect = (): void => controller.abort();
	const statusChanged: HostSlot = (_sender, status) => {
		if (
			status === "restarting" ||
			status === "autorestarting" ||
			status === "terminating" ||
			status === "dead"
		)
			disconnect();
	};
	const subscriptions: Array<{ signal: HostSignal; slot: HostSlot }> = [];
	const cleanup = (): void => {
		model.off("comm:close", disconnect);
		for (const subscription of subscriptions) subscription.signal.disconnect(subscription.slot);
		signal.removeEventListener("abort", cleanup);
	};
	signal.addEventListener("abort", cleanup, { once: true });
	model.on("comm:close", disconnect);
	const manager = model.widget_manager;
	if (!("kernel" in manager)) return signal;
	const kernel = manager.kernel;
	if (kernel === null) {
		disconnect();
		return signal;
	}
	// eslint-disable-next-line anti-slop/no-runtime-typeof -- Native kernel connections expose their lifecycle on the prototype.
	if (typeof kernel !== "object" || kernel === null) return signal;
	if ("isDisposed" in kernel && kernel.isDisposed === true) {
		disconnect();
		return signal;
	}
	if ("disposed" in kernel && isHostSignal(kernel.disposed)) {
		kernel.disposed.connect(disconnect);
		subscriptions.push({ signal: kernel.disposed, slot: disconnect });
	}
	if ("statusChanged" in kernel && isHostSignal(kernel.statusChanged)) {
		kernel.statusChanged.connect(statusChanged);
		subscriptions.push({ signal: kernel.statusChanged, slot: statusChanged });
	}
	if ("status" in kernel && isString(kernel.status)) statusChanged(undefined, kernel.status);
	return signal;
}

function isHostSignal<Value>(value: Value): value is Value & HostSignal {
	return (
		// eslint-disable-next-line anti-slop/no-runtime-typeof -- Lumino signals are native objects with prototype methods.
		typeof value === "object" &&
		value !== null &&
		"connect" in value &&
		isCallable(value.connect) &&
		"disconnect" in value &&
		isCallable(value.disconnect)
	);
}
