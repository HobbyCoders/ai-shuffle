import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { PreferenceAdapter } from '$lib/services/preference-sync';
import { createPersistedStore, persisted, resolvePreferenceKey } from './persisted';

const singleton = vi.hoisted(() => ({ register: vi.fn(), save: vi.fn() }));
vi.mock('$lib/services/preferences', () => ({ preferenceSync: singleton }));
beforeEach(() => { localStorage.clear(); singleton.register.mockClear(); singleton.save.mockClear(); });

function fakeSync() {
	const adapters = new Map<string, PreferenceAdapter>();
	return { adapters, register: vi.fn((key: string, adapter: PreferenceAdapter) => { adapters.set(key, adapter); }), save: vi.fn() };
}
const throwing = () => { throw new Error('blocked'); };
const blocked = { getItem: throwing, setItem: throwing, removeItem: throwing, key: throwing, clear: throwing, length: 0 } as unknown as Storage;

describe('persisted stores', () => {
	it('uses keys verbatim unless ui namespaces them, and caches under ai-shuffle:<key>', () => {
		expect(resolvePreferenceKey('context_breakdown_open')).toBe('context_breakdown_open');
		expect(resolvePreferenceKey('runner_pick', true)).toBe('ui:runner_pick');
		expect(resolvePreferenceKey('ui:runner_pick', true)).toBe('ui:runner_pick');
		const sync = fakeSync();
		const store = createPersistedStore({ key: 'runner_pick', ui: true, initial: 'a' }, sync, localStorage);
		expect(store.key).toBe('ui:runner_pick');
		expect([...sync.adapters.keys()]).toEqual(['ui:runner_pick']);
		store.set('b');
		expect(localStorage.getItem('ai-shuffle:ui:runner_pick')).toBe('"b"');
		expect(sync.save).toHaveBeenCalledExactlyOnceWith('ui:runner_pick', 'b');
	});
	it('loads the cache synchronously and retires legacy keys without importing them', () => {
		localStorage.setItem('ai-shuffle:tool', '"hooks"');
		localStorage.setItem('old-tool', 'mcp');
		const sync = fakeSync();
		const store = createPersistedStore({ key: 'tool', initial: 'profile', legacyKeys: ['old-tool'] }, sync, localStorage);
		expect(store.current).toBe('hooks');
		expect(localStorage.getItem('old-tool')).toBeNull();
		expect(sync.save).not.toHaveBeenCalled();
	});
	it('imports the first legacy key found (JSON or raw string), removes them all and writes the cache', () => {
		localStorage.setItem('old-b', '{"a":1}');
		localStorage.setItem('old-c', '{"a":2}');
		localStorage.setItem('old-tab', 'hooks');
		const sync = fakeSync();
		const prefs = createPersistedStore({ key: 'prefs', initial: { a: 0, b: '' }, legacyKeys: ['old-a', 'old-b', 'old-c'] }, sync, localStorage);
		expect(prefs.current).toEqual({ a: 1, b: '' });
		expect(JSON.parse(localStorage.getItem('ai-shuffle:prefs')!)).toEqual({ a: 1, b: '' });
		expect(localStorage.getItem('old-b')).toBeNull();
		expect(localStorage.getItem('old-c')).toBeNull();
		const tab = createPersistedStore({ key: 'tab', initial: 'profile', legacyKeys: ['old-tab'] }, sync, localStorage);
		expect(tab.current).toBe('hooks');
		expect(localStorage.getItem('ai-shuffle:tab')).toBe('"hooks"');
		expect(sync.save).not.toHaveBeenCalled();
	});
	it('runs normalize on cached, legacy and server values, falling back to initial on undefined', () => {
		localStorage.setItem('ai-shuffle:mode', '"stale"');
		localStorage.setItem('old-count', '2');
		const sync = fakeSync();
		const mode = createPersistedStore({ key: 'mode', initial: 'a', normalize: v => v === 'a' || v === 'b' ? v : undefined }, sync, localStorage);
		expect(mode.current).toBe('a');
		sync.adapters.get('mode')!.apply('b');
		expect(mode.current).toBe('b');
		sync.adapters.get('mode')!.apply('nope');
		expect(mode.current).toBe('a');
		const count = createPersistedStore({ key: 'count', initial: 'zero', legacyKeys: ['old-count'],
			normalize: v => typeof v === 'number' ? ['zero', 'one', 'two'][v] : typeof v === 'string' ? v : undefined }, sync, localStorage);
		expect(count.current).toBe('two');
		expect(localStorage.getItem('ai-shuffle:count')).toBe('"two"');
	});
	it('without normalize, accepts same-typed values and deep-merges objects over initial', () => {
		const sync = fakeSync();
		const shape = createPersistedStore({ key: 'shape', initial: { open: true, width: 10, nested: { x: 1 } } }, sync, localStorage);
		sync.adapters.get('shape')!.apply({ open: 'yes', width: 20, extra: 1, nested: { y: 2 } });
		expect(shape.current).toEqual({ open: true, width: 20, extra: 1, nested: { x: 1, y: 2 } });
		const size = createPersistedStore({ key: 'size', initial: 3 }, sync, localStorage);
		sync.adapters.get('size')!.apply('4');
		expect(size.current).toBe(3);
		sync.adapters.get('size')!.apply(5);
		expect(size.current).toBe(5);
		const list = createPersistedStore<number[]>({ key: 'list', initial: [] }, sync, localStorage);
		sync.adapters.get('list')!.apply('x');
		expect(list.current).toEqual([]);
		sync.adapters.get('list')!.apply([1]);
		expect(list.current).toEqual([1]);
	});
	it('writes through memory, cache and sync on set/update and notifies subscribers', () => {
		const sync = fakeSync();
		const store = createPersistedStore({ key: 'counter', initial: { n: 0 } }, sync, localStorage);
		const seen: unknown[] = [];
		const stop = store.subscribe(value => seen.push(value));
		store.set({ n: 1 });
		expect(store.current).toEqual({ n: 1 });
		expect(JSON.parse(localStorage.getItem('ai-shuffle:counter')!)).toEqual({ n: 1 });
		expect(sync.save).toHaveBeenLastCalledWith('counter', { n: 1 });
		store.update(value => ({ n: value.n + 1 }));
		expect(sync.adapters.get('counter')!.read()).toEqual({ n: 2 });
		expect(sync.save).toHaveBeenLastCalledWith('counter', { n: 2 });
		expect(seen).toEqual([{ n: 0 }, { n: 1 }, { n: 2 }]);
		stop();
	});
	it('applies server values and account resets without echo; reset() is an edit', () => {
		const initial = { n: 0 };
		const sync = fakeSync();
		const store = createPersistedStore({ key: 'counter', initial }, sync, localStorage);
		store.set({ n: 1 });
		sync.save.mockClear();
		const adapter = sync.adapters.get('counter')!;
		adapter.apply({ n: 5 });
		expect(store.current).toEqual({ n: 5 });
		expect(JSON.parse(localStorage.getItem('ai-shuffle:counter')!)).toEqual({ n: 5 });
		adapter.apply(undefined);
		expect(store.current).toEqual({ n: 0 });
		expect(store.current).not.toBe(initial);
		expect(JSON.parse(localStorage.getItem('ai-shuffle:counter')!)).toEqual({ n: 0 });
		expect(sync.save).not.toHaveBeenCalled();
		store.set({ n: 2 });
		store.reset();
		expect(store.current).toEqual({ n: 0 });
		expect(sync.save).toHaveBeenLastCalledWith('counter', { n: 0 });
	});
	it('keeps device-local stores in the cache only, never in the sync engine', () => {
		localStorage.setItem('ai-shuffle:device', '"d-1"');
		const sync = fakeSync();
		const store = createPersistedStore({ key: 'device', initial: '', local: true }, sync, localStorage);
		expect(store.current).toBe('d-1');
		store.set('d-2');
		store.update(id => id + '!');
		store.reset();
		expect(localStorage.getItem('ai-shuffle:device')).toBe('""');
		expect(sync.register).not.toHaveBeenCalled();
		expect(sync.save).not.toHaveBeenCalled();
	});
	it('stays usable when storage throws or is absent', () => {
		const sync = fakeSync();
		const store = createPersistedStore({ key: 'blocked', initial: { a: 1 }, legacyKeys: ['old'] }, sync, blocked);
		expect(store.current).toEqual({ a: 1 });
		store.set({ a: 2 });
		expect(store.current).toEqual({ a: 2 });
		expect(sync.save).toHaveBeenCalledExactlyOnceWith('blocked', { a: 2 });
		sync.adapters.get('blocked')!.apply({ a: 3 });
		expect(store.current).toEqual({ a: 3 });
		store.reset();
		expect(store.current).toEqual({ a: 1 });
		const ssr = createPersistedStore({ key: 'ssr', initial: 'x', legacyKeys: ['old'] }, sync);
		ssr.set('y');
		expect(ssr.current).toBe('y');
		expect(sync.save).toHaveBeenLastCalledWith('ssr', 'y');
	});
	it('lets a custom cache layout replace the single JSON key', () => {
		localStorage.setItem('flag', '0');
		const sync = fakeSync();
		const store = createPersistedStore<boolean>({ key: 'flag', initial: true, cache: {
			read: s => { const stored = s.getItem('flag'); return stored === null ? undefined : stored !== '0'; },
			write: (s, open) => s.setItem('flag', open ? '1' : '0')
		} }, sync, localStorage);
		expect(store.current).toBe(false);
		store.set(true);
		expect(localStorage.getItem('flag')).toBe('1');
		expect(localStorage.getItem('ai-shuffle:flag')).toBeNull();
		sync.adapters.get('flag')!.apply(undefined);
		expect(localStorage.getItem('flag')).toBe('1');
	});
	it('persisted() wires the singleton sync engine and this browser\'s localStorage', () => {
		localStorage.setItem('ai-shuffle:ui:pick', '"b"');
		const store = persisted({ key: 'pick', ui: true, initial: 'a' });
		expect(store.current).toBe('b');
		expect(singleton.register).toHaveBeenCalledExactlyOnceWith('ui:pick', expect.objectContaining({ read: expect.any(Function), apply: expect.any(Function) }));
		store.set('c');
		expect(localStorage.getItem('ai-shuffle:ui:pick')).toBe('"c"');
		expect(singleton.save).toHaveBeenCalledExactlyOnceWith('ui:pick', 'c');
	});
});
