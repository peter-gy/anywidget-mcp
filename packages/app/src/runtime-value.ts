export type RuntimeValue =
	| string
	| number
	| boolean
	| null
	| undefined
	| ArrayBuffer
	| ArrayBufferView
	| RuntimeValue[]
	| RuntimeRecord;

export interface RuntimeRecord {
	[key: string]: RuntimeValue;
}

export type WidgetValue = object | string | number | boolean | bigint | symbol | null | undefined;

export function isRecord<Value>(value: Value): value is Value & RuntimeRecord {
	return isPlainObject(value);
}

export function isPlainObject<Value>(value: Value): value is Value & object {
	if (Object(value) !== value || Array.isArray(value)) return false;
	const prototype = Object.getPrototypeOf(value);
	return prototype === Object.prototype || prototype === null;
}

export function isString<Value>(value: Value): value is Value & string {
	return Object(value) !== value && Object.getPrototypeOf(Object(value)) === String.prototype;
}

export function isNumber<Value>(value: Value): value is Value & number {
	return Object(value) !== value && Object.getPrototypeOf(Object(value)) === Number.prototype;
}

export function isBoolean<Value>(value: Value): value is Value & boolean {
	return Object(value) !== value && Object.getPrototypeOf(Object(value)) === Boolean.prototype;
}

export function isCallable<Value>(
	value: Value,
): value is Value & ((...args: WidgetValue[]) => WidgetValue) {
	return value instanceof Function;
}
