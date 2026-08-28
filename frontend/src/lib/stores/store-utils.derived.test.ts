/**
 * Derived-store equality short-circuit (finding W10 / R1).
 *
 * `activeSession` notifies every subscriber on every write — many times a second
 * while a turn streams, with 17 components subscribed. A derived store must
 * re-invoke its own subscriber ONLY when the value it selects actually changed,
 * so a component that needs one field does no work while another field churns.
 */

import { describe, it, expect, vi } from 'vitest';
import { createStore, shallowEqual } from './store-utils';

interface S {
	messages: string[];
	isStreaming: boolean;
	title: string;
	tokens: number;
}

function makeStore() {
	return createStore<S>({ messages: [], isStreaming: false, title: 'New Chat', tokens: 0 });
}

describe('createDerived equality short-circuit', () => {
	it('does not notify when the derived value is unchanged', () => {
		const store = makeStore();
		const streaming = store.createDerived((s) => s.isStreaming);
		const seen = vi.fn();
		streaming.subscribe(seen);
		expect(seen).toHaveBeenCalledTimes(1); // initial value on subscribe

		// A streaming fold: messages churn, the flag does not.
		for (let i = 0; i < 20; i++) {
			store.update((s) => ({ ...s, messages: [...s.messages, `tok${i}`] }));
		}
		expect(seen).toHaveBeenCalledTimes(1);

		// …and a real change still lands, exactly once.
		store.update((s) => ({ ...s, isStreaming: true }));
		expect(seen).toHaveBeenCalledTimes(2);
		expect(seen).toHaveBeenLastCalledWith(true);

		store.update((s) => ({ ...s, isStreaming: true, title: 'again' }));
		expect(seen).toHaveBeenCalledTimes(2);
	});

	it('is per-subscriber: two subscribers to the same derived both stay correct', () => {
		const store = makeStore();
		const title = store.createDerived((s) => s.title);
		const a = vi.fn();
		const b = vi.fn();
		title.subscribe(a);
		store.update((s) => ({ ...s, title: 'first' }));
		title.subscribe(b);
		store.update((s) => ({ ...s, tokens: 5 }));

		expect(a).toHaveBeenCalledTimes(2); // initial + 'first'
		expect(b).toHaveBeenCalledTimes(1); // initial ('first') only
		expect(b).toHaveBeenLastCalledWith('first');
	});

	it('unsubscribing stops notifications', () => {
		const store = makeStore();
		const tokens = store.createDerived((s) => s.tokens);
		const seen = vi.fn();
		const off = tokens.subscribe(seen);
		store.update((s) => ({ ...s, tokens: 1 }));
		off();
		store.update((s) => ({ ...s, tokens: 2 }));
		expect(seen).toHaveBeenCalledTimes(2);
		expect(tokens.current).toBe(2);
	});

	it('a getter that BUILDS an object needs shallowEqual to short-circuit', () => {
		const store = makeStore();
		const identity = (s: S) => ({ title: s.title, streaming: s.isStreaming });

		const naive = store.createDerived(identity);
		const shallow = store.createDerived(identity, shallowEqual);
		const naiveSeen = vi.fn();
		const shallowSeen = vi.fn();
		naive.subscribe(naiveSeen);
		shallow.subscribe(shallowSeen);

		for (let i = 0; i < 10; i++) {
			store.update((s) => ({ ...s, tokens: s.tokens + 1 }));
		}
		// A fresh object is never Object.is-equal, so the default check cannot
		// help — which is exactly why the equality predicate is pluggable.
		expect(naiveSeen).toHaveBeenCalledTimes(11);
		expect(shallowSeen).toHaveBeenCalledTimes(1);

		store.update((s) => ({ ...s, title: 'changed' }));
		expect(shallowSeen).toHaveBeenCalledTimes(2);
	});
});

describe('shallowEqual', () => {
	it('compares own keys by identity', () => {
		expect(shallowEqual({ a: 1, b: 'x' }, { a: 1, b: 'x' })).toBe(true);
		expect(shallowEqual({ a: 1 }, { a: 2 })).toBe(false);
		expect(shallowEqual({ a: 1 }, { a: 1, b: 2 })).toBe(false);
		expect(shallowEqual({ a: 1, b: 2 }, { a: 1 })).toBe(false);
		const arr = [1];
		expect(shallowEqual({ a: arr }, { a: arr })).toBe(true);
		expect(shallowEqual({ a: [1] }, { a: [1] })).toBe(false); // nested identity
	});

	it('handles primitives, null and arrays', () => {
		expect(shallowEqual(1, 1)).toBe(true);
		expect(shallowEqual('a', 'b')).toBe(false);
		expect(shallowEqual(null, null)).toBe(true);
		expect(shallowEqual(null, {} as unknown as null)).toBe(false);
		expect(shallowEqual([1, 2], [1, 2])).toBe(true);
		expect(shallowEqual([1, 2], [2, 1])).toBe(false);
		expect(shallowEqual([1], { 0: 1 } as unknown as number[])).toBe(false);
	});
});
