import { describe, expect, it } from 'vitest';
import { packProjection, ProjectionCodecError, unpackProjection, type PackedValue } from './transcript-codec';
import { reduceAll, withLogEpoch, type WireEvent } from './reducer';

function roundTrip(value: unknown): unknown {
	// The durable JSON/JSONB boundary is real here, not a JS object clone.
	return unpackProjection(JSON.parse(JSON.stringify(packProjection(value))));
}

describe('local transcript checkpoint codec', () => {
	it('preserves explicit undefined, Map insertion order and all numeric values', () => {
		const value = { z: undefined, a: new Map([['last', undefined], ['first', -0]]),
			numbers: [NaN, Infinity, -Infinity, -0], flags: new Set(['b', 'a']) };
		const decoded = roundTrip(value) as typeof value;
		expect(decoded).toStrictEqual(value);
		expect(Object.keys(decoded)).toEqual(Object.keys(value));
		expect([...decoded.a]).toStrictEqual([...value.a]);
		expect([...decoded.flags]).toStrictEqual([...value.flags]);
		expect(Object.hasOwn(decoded, 'z')).toBe(true);
		expect(Object.is(decoded.numbers[3], -0)).toBe(true);
	});

	it('keeps tag-shaped user objects and prototype-sensitive keys as data', () => {
		const value = JSON.parse('{"t":"undefined","__proto__":{"polluted":true},"constructor":"data"}');
		const decoded = roundTrip(value) as Record<string, unknown>;
		expect(decoded).toStrictEqual(value);
		expect(Object.getPrototypeOf(decoded)).toBe(Object.prototype);
		expect(Object.hasOwn(decoded, '__proto__')).toBe(true);
		expect(({} as Record<string, unknown>).polluted).toBeUndefined();
	});

	it('keeps sparse arrays distinct from explicit undefined and preserves null prototypes', () => {
		const sparse = new Array(3);
		sparse[1] = undefined;
		const record = Object.create(null);
		record.value = sparse;
		const decoded = roundTrip(record) as typeof record;
		expect(Object.getPrototypeOf(decoded)).toBeNull();
		expect(0 in decoded.value).toBe(false);
		expect(1 in decoded.value).toBe(true);
		expect(decoded.value).toStrictEqual(sparse);
	});

	it('round-trips the actual full reducer state without normalizing fields or origins', () => {
		const event = (seq: number, type: string, payload: Record<string, unknown>): WireEvent => ({
			id: `event-${seq}`, seq, type, created_at: '2026-10-30T00:00:00Z', ...payload
		});
		const state = withLogEpoch(reduceAll([
			event(1, 'message.user', { content: 'Run it' }),
			event(2, 'tool.use', { name: 'Bash', tool_use_id: 'tool', input: { command: 'pwd' } }),
			event(3, 'tool.result', { tool_use_id: 'tool', content: 'done' }),
			event(4, 'assistant.text_delta', { text: 'Finished' }),
			event(5, 'turn.completed', {})
		]), 'source-epoch');
		const decoded = roundTrip(state) as typeof state;
		expect(decoded).toStrictEqual(state);
		expect([...decoded.messageOriginSeq]).toStrictEqual([...state.messageOriginSeq]);
		expect(decoded.messages.map(Object.keys)).toStrictEqual(state.messages.map(Object.keys));
		decoded.messageOriginSeq.set('new', 99);
		expect(state.messageOriginSeq.has('new')).toBe(false);
	});

	it('fails closed on unsupported graphs and malformed durable tags', () => {
		const cyclic: Record<string, unknown> = {};
		cyclic.self = cyclic;
		for (const value of [cyclic, new Date(), () => 1, Symbol('unsupported')]) {
			expect(() => packProjection(value)).toThrow(ProjectionCodecError);
		}
		expect(() => unpackProjection({ t: 'number', v: 'unknown' })).toThrow('invalid_number');
		expect(() => unpackProjection({ t: 'object', p: 'plain', v: [['x', 1], ['x', 2]] })).toThrow('invalid_key');
		expect(() => unpackProjection({ t: 'unknown' } as unknown as PackedValue)).toThrow(ProjectionCodecError);
	});
});
