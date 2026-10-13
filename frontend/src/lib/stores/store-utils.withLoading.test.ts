/**
 * Tests for the withLoading store utility helper.
 *
 * Verifies that:
 * - loading is set to true before the operation starts
 * - loading is cleared (false) on success
 * - loading is cleared (false) on throw, and error is set
 * - the resolved value is propagated on success
 * - errors are re-thrown to the caller
 * - both string and function errorMessage forms are handled
 */

import { describe, it, expect } from 'vitest';
import { withLoading } from './store-utils';
import type { LoadingState } from './store-utils';

// ─── Helpers ─────────────────────────────────────────────────────────────────

interface TestState extends LoadingState {
	value: number;
}

function makeStore(initial: TestState) {
	let state = { ...initial };
	const calls: Array<{ loading: boolean; error: string | null }> = [];

	function update(fn: (s: TestState) => TestState) {
		state = fn(state);
		calls.push({ loading: state.loading, error: state.error });
	}

	return { getState: () => state, calls, update };
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe('withLoading', () => {
	it('sets loading=true before operation, clears to false on success', async () => {
		const store = makeStore({ loading: false, error: null, value: 0 });
		let loadingDuringOperation = false;

		await withLoading(
			store.update,
			async () => {
				loadingDuringOperation = store.getState().loading;
				return 42;
			},
			'fallback'
		);

		expect(loadingDuringOperation).toBe(true);
		expect(store.getState().loading).toBe(false);
		expect(store.getState().error).toBeNull();
	});

	it('returns the resolved value from the operation', async () => {
		const store = makeStore({ loading: false, error: null, value: 0 });

		const result = await withLoading(store.update, async () => 'hello', 'fallback');

		expect(result).toBe('hello');
	});

	it('clears loading=false and sets error on throw', async () => {
		const store = makeStore({ loading: false, error: null, value: 0 });

		await expect(
			withLoading(
				store.update,
				async () => { throw new Error('boom'); },
				'Operation failed'
			)
		).rejects.toThrow('boom');

		expect(store.getState().loading).toBe(false);
		expect(store.getState().error).toBe('boom');
	});

	it('uses the fallback string when the thrown value is not an Error instance', async () => {
		const store = makeStore({ loading: false, error: null, value: 0 });

		// eslint-disable-next-line @typescript-eslint/only-throw-error
		await expect(
			withLoading(store.update, async () => { throw 'a plain string'; }, 'Fallback message')
		).rejects.toBe('a plain string');

		expect(store.getState().error).toBe('Fallback message');
	});

	it('re-throws the original error so callers can handle it', async () => {
		const store = makeStore({ loading: false, error: null, value: 0 });
		const sentinel = new Error('sentinel');

		const thrown = await withLoading(
			store.update,
			async () => { throw sentinel; },
			'fallback'
		).catch((e) => e);

		expect(thrown).toBe(sentinel);
	});

	it('accepts a function for errorMessage (custom extraction)', async () => {
		const store = makeStore({ loading: false, error: null, value: 0 });

		await expect(
			withLoading(
				store.update,
				async () => { throw { detail: 'custom detail' }; },
				(e) => (e && typeof e === 'object' && 'detail' in e
					? (e as { detail: string }).detail
					: 'fallback')
			)
		).rejects.toEqual({ detail: 'custom detail' });

		expect(store.getState().error).toBe('custom detail');
	});

	it('update is called exactly twice: once before (loading=true), once after', async () => {
		const store = makeStore({ loading: false, error: null, value: 0 });

		await withLoading(store.update, async () => 'ok', 'err');

		expect(store.calls).toHaveLength(2);
		expect(store.calls[0]).toEqual({ loading: true, error: null });
		expect(store.calls[1]).toEqual({ loading: false, error: null });
	});

	it('on throw: update is called twice — set loading=true then set error+loading=false', async () => {
		const store = makeStore({ loading: false, error: null, value: 0 });

		await withLoading(
			store.update,
			async () => { throw new Error('fail'); },
			'Operation failed'
		).catch(() => {});

		expect(store.calls).toHaveLength(2);
		expect(store.calls[0]).toEqual({ loading: true, error: null });
		expect(store.calls[1]).toEqual({ loading: false, error: 'fail' });
	});
});
