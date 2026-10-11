import { describe, expect, it } from 'vitest';
import { createFoldState, FOLD_STATE_LIMIT } from './fold-state';

function memoryStorage(): Storage {
	const data = new Map<string, string>();
	return {
		get length() { return data.size; },
		clear: () => data.clear(),
		getItem: (key) => data.get(key) ?? null,
		key: (index) => [...data.keys()][index] ?? null,
		removeItem: (key) => { data.delete(key); },
		setItem: (key, value) => { data.set(key, String(value)); }
	};
}

describe('fold state', () => {
	it('records open folds only, closed being the default', () => {
		const folds = createFoldState(memoryStorage());
		expect(folds.isOpen('t:0')).toBe(false);
		folds.setOpen('t:0', true);
		folds.setOpen('t:turn', true);
		expect(folds.isOpen('t:0')).toBe(true);
		folds.setOpen('t:0', false);
		expect(folds.current).toEqual({ 't:turn': true });
	});

	it('restores open folds after a reload (a new store on the same storage)', () => {
		const storage = memoryStorage();
		createFoldState(storage).setOpen('srv-1:2', true);
		const reloaded = createFoldState(storage);
		expect(reloaded.isOpen('srv-1:2')).toBe(true);
		expect(reloaded.isOpen('srv-1:1')).toBe(false);
	});

	it('moves an optimistic turn onto its server id, keeping every fold, and survives a reload', () => {
		const storage = memoryStorage();
		const folds = createFoldState(storage);
		folds.setOpen('local-1:run-r1', true);
		folds.setOpen('local-1:turn', true);
		folds.setOpen('other:0', true);
		folds.remap('local-1', 'srv-1');
		expect(folds.current).toEqual({ 'srv-1:run-r1': true, 'srv-1:turn': true, 'other:0': true });
		// A prefix that only looks alike is not the same turn.
		folds.setOpen('local-10:0', true);
		folds.remap('local-1', 'srv-2');
		expect(folds.isOpen('local-10:0')).toBe(true);
		expect(createFoldState(storage).current).toEqual(folds.current);
	});

	it('merges an open run into the settled turn fold, stored for a reload', () => {
		const storage = memoryStorage();
		const folds = createFoldState(storage);
		folds.setOpen('t1:run-r1', true);
		folds.setOpen('t1:run-r2', true);
		folds.setOpen('t10:run-r1', true);
		folds.promoteRuns('t1');
		expect(folds.current).toEqual({ 't10:run-r1': true, 't1:turn': true });
		expect(createFoldState(storage).current).toEqual(folds.current);
		// No open run: the settled fold keeps its own state (closed by default).
		folds.promoteRuns('t2');
		expect(folds.isOpen('t2:turn')).toBe(false);
	});

	it('drops the oldest folds past its cap', () => {
		const folds = createFoldState(memoryStorage());
		for (let i = 0; i <= FOLD_STATE_LIMIT; i++) folds.setOpen(`t${i}:0`, true);
		expect(Object.keys(folds.current)).toHaveLength(FOLD_STATE_LIMIT);
		expect(folds.isOpen('t0:0')).toBe(false);
		expect(folds.isOpen(`t${FOLD_STATE_LIMIT}:0`)).toBe(true);
	});

	it('ignores a corrupt cache', () => {
		const storage = memoryStorage();
		createFoldState(storage).setOpen('seed:0', true);
		const cacheKey = storage.key(0)!;
		storage.setItem(cacheKey, '{"a:0":true,"b:0":"yes"}');
		expect(createFoldState(storage).current).toEqual({ 'a:0': true });
		storage.setItem(cacheKey, 'not json');
		expect(createFoldState(storage).current).toEqual({});
	});
});
