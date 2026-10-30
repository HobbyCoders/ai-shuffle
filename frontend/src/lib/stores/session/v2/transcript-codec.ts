/** Lossless data encoding for installation-local reducer checkpoints.
 * JSONB may reorder object keys and JSON drops undefined/non-finite values.
 * Tagged entry arrays preserve the reducer's actual fields and Map order;
 * user objects cannot collide with the tags. Unsupported graphs fail closed.
 */
export type PackedValue = null | boolean | number | string | {
	t: 'undefined' | 'hole' | 'number' | 'array' | 'object' | 'map' | 'set';
	v?: PackedValue[] | Array<[PackedValue, PackedValue]> | string;
	p?: 'plain' | 'null';
};

export class ProjectionCodecError extends Error {
	constructor(readonly code: string) { super(code); this.name = 'ProjectionCodecError'; }
}

const MAX_DEPTH = 128;
const MAX_NODES = 1_000_000;

export function packProjection(value: unknown): PackedValue {
	let nodes = 0;
	const ancestors = new Set<object>();
	function pack(item: unknown, depth: number): PackedValue {
		if (++nodes > MAX_NODES || depth > MAX_DEPTH) throw new ProjectionCodecError('codec_limit');
		if (item === undefined) return { t: 'undefined' };
		if (item === null || typeof item === 'string' || typeof item === 'boolean') return item;
		if (typeof item === 'number') {
			if (Number.isNaN(item)) return { t: 'number', v: 'nan' };
			if (!Number.isFinite(item)) return { t: 'number', v: item > 0 ? 'infinity' : '-infinity' };
			return Object.is(item, -0) ? { t: 'number', v: '-0' } : item;
		}
		if (typeof item !== 'object' || ancestors.has(item)) throw new ProjectionCodecError('unsupported_value');
		if (Object.getOwnPropertySymbols(item).length) throw new ProjectionCodecError('unsupported_property');
		ancestors.add(item);
		try {
			if (item instanceof Map) return { t: 'map', v: [...item].map(([k, v]): [PackedValue, PackedValue] => [pack(k, depth + 1), pack(v, depth + 1)]) };
			if (item instanceof Set) return { t: 'set', v: [...item].map(v => pack(v, depth + 1)) };
			if (Array.isArray(item)) {
				if (item.length > MAX_NODES || Object.keys(item).some(k => !/^(0|[1-9][0-9]*)$/.test(k) || Number(k) >= item.length)) {
					throw new ProjectionCodecError('unsupported_array');
				}
				return { t: 'array', v: Array.from({ length: item.length }, (_, i): PackedValue =>
					Object.hasOwn(item, i) ? pack(item[i], depth + 1) : { t: 'hole' }) };
			}
			const prototype = Object.getPrototypeOf(item);
			if (prototype !== Object.prototype && prototype !== null) throw new ProjectionCodecError('unsupported_class');
			const entries: Array<[PackedValue, PackedValue]> = [];
			for (const [key, descriptor] of Object.entries(Object.getOwnPropertyDescriptors(item))) {
				if (!descriptor.enumerable || !('value' in descriptor)) throw new ProjectionCodecError('unsupported_property');
				entries.push([key, pack(descriptor.value, depth + 1)]);
			}
			return { t: 'object', p: prototype === null ? 'null' : 'plain', v: entries };
		} finally { ancestors.delete(item); }
	}
	return pack(value, 0);
}

export function unpackProjection(value: PackedValue): unknown {
	let nodes = 0;
	function unpack(item: PackedValue, depth: number): unknown {
		if (++nodes > MAX_NODES || depth > MAX_DEPTH) throw new ProjectionCodecError('codec_limit');
		if (item === null || typeof item === 'string' || typeof item === 'boolean' || typeof item === 'number') return item;
		if (typeof item !== 'object' || Array.isArray(item)) throw new ProjectionCodecError('invalid_tag');
		if (item.t === 'undefined') return undefined;
		if (item.t === 'number') {
			if (item.v === 'nan') return NaN;
			if (item.v === 'infinity') return Infinity;
			if (item.v === '-infinity') return -Infinity;
			if (item.v === '-0') return -0;
			throw new ProjectionCodecError('invalid_number');
		}
		if (!Array.isArray(item.v) || item.v.length > MAX_NODES) throw new ProjectionCodecError('invalid_entries');
		if (item.t === 'array') {
			const result: unknown[] = new Array(item.v.length);
			for (let i = 0; i < item.v.length; i++) {
				const entry = item.v[i] as PackedValue;
				if (entry && typeof entry === 'object' && entry.t === 'hole') continue;
				result[i] = unpack(entry, depth + 1);
			}
			return result;
		}
		if (item.t === 'set') return new Set((item.v as PackedValue[]).map(v => unpack(v, depth + 1)));
		if (item.t !== 'map' && item.t !== 'object') throw new ProjectionCodecError('invalid_tag');
		const entries: Array<[unknown, unknown]> = [];
		for (const entry of item.v) {
			if (!Array.isArray(entry) || entry.length !== 2) throw new ProjectionCodecError('invalid_entry');
			entries.push([unpack(entry[0], depth + 1), unpack(entry[1], depth + 1)]);
		}
		if (item.t === 'map') return new Map(entries);
		if (item.p !== 'plain' && item.p !== 'null') throw new ProjectionCodecError('invalid_prototype');
		const result = item.p === 'null' ? Object.create(null) : {};
		const seen = new Set<string>();
		for (const [key, v] of entries) {
			if (typeof key !== 'string' || seen.has(key)) throw new ProjectionCodecError('invalid_key');
			seen.add(key);
			Object.defineProperty(result, key, { value: v, enumerable: true, writable: true, configurable: true });
		}
		return result;
	}
	return unpack(value, 0);
}
