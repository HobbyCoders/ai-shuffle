import { describe, expect, it } from 'vitest';
import { permitsOversizeOriginSettle } from '../../../../tests/journeys/lib/transcript-settle.mjs';

/** @returns {Parameters<typeof permitsOversizeOriginSettle>[0]} */
const measured = () => ({ phone: true, before: { top: 96.703125 }, after: { rows: 1, maxDrift: 16.703125 },
	settle: { total: 0.08448212182280444,
		entries: [{ value: 0.08448212182280444, transition: 0, scrollTop: 0, singleton: true, virtualizedOnly: true }],
		transitions: [{ id: 0, fromRows: 2, toRows: 1, reader: true, scrollTop: 0 }] } });

describe('approved oversize origin settle', () => {
	it('accepts the measured single phone transition', () => expect(permitsOversizeOriginSettle(measured())).toBe(true));
	/** @type {Array<[string, (sample: ReturnType<typeof measured>) => void]>} */
	const rejected = [
		['desktop', sample => { sample.phone = false; }],
		['non-origin entry', sample => { sample.settle.entries[0].scrollTop = 500; }],
		['non-origin transition', sample => { sample.settle.transitions[0].scrollTop = 500; }],
		['singleton state without a transition', sample => { sample.settle.transitions = []; }],
		['already-singleton state', sample => { sample.settle.transitions[0].fromRows = 1; }],
		['repeated singleton transition', sample => { sample.settle.transitions.push({ ...sample.settle.transitions[0], id: 1 }); }],
		['unrelated frame', sample => { sample.settle.entries[0].transition = null; }],
		['two sub-threshold shifts', sample => {
			sample.settle.entries = [0.04, 0.04].map(value => ({ ...sample.settle.entries[0], value })); sample.settle.total = 0.08;
		}],
		['extra tiny shift', sample => { sample.settle.entries.push({ ...sample.settle.entries[0], value: 0.000001 }); }],
		['unrelated moved node', sample => { sample.settle.entries[0].virtualizedOnly = false; }],
		['wrong singleton', sample => { sample.settle.transitions[0].reader = false; }],
		['excessive drift', sample => { sample.after.maxDrift = 20.01; }],
		['CLS at the limit', sample => { sample.settle.total = 0.1; }]
	];
	it.each(rejected)('rejects %s', (_name, mutate) => {
		const sample = measured(); mutate(sample); expect(permitsOversizeOriginSettle(sample)).toBe(false);
	});
});
