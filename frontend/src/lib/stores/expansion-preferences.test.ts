import { beforeEach, describe, expect, it, vi } from 'vitest';
import { PreferenceSync, type PreferenceAdapter } from '$lib/services/preference-sync';
import { createExpansionPreferences } from './expansion-preferences';

beforeEach(() => localStorage.clear());
const deferred = <T>() => {
	let resolve!: (value: T) => void;
	const promise = new Promise<T>(r => { resolve = r; });
	return { promise, resolve };
};
function fixture() {
	const adapters = new Map<string, PreferenceAdapter>();
	const save = vi.fn();
	const stores = createExpansionPreferences({ register: (key, value) => adapters.set(key, value), save }, localStorage);
	return { ...stores, adapters, save };
}

describe('account expansion preferences', () => {
	it('syncs hide and restore edits between two account clients through the transport', async () => {
		const server: Record<string, unknown> = {};
		const transport = {
			all: async () => structuredClone(server),
			get: async (key: string) => key in server ? { value: structuredClone(server[key]) } : null,
			put: vi.fn(async (key: string, value: unknown, onlyIfMissing?: boolean) => {
				if (!onlyIfMissing || !(key in server)) server[key] = structuredClone(value);
				return { value: structuredClone(server[key]) };
			})
		};
		const syncA = new PreferenceSync(transport);
		const syncB = new PreferenceSync(transport);
		const a = createExpansionPreferences(syncA).hiddenProviders;
		const b = createExpansionPreferences(syncB).hiddenProviders;
		syncA.start(false, 'account:A');
		syncB.start(false, 'account:A');
		await syncA.reconnect();
		await syncB.reconnect();
		a.set({ plan: true, 'provider:api': true });
		await vi.waitFor(() => expect(server.context_usage_hidden_providers).toEqual({ plan: true, 'provider:api': true }));
		await syncB.refresh('context_usage_hidden_providers');
		expect(b.current).toEqual(a.current);
		b.set({ plan: true });
		await vi.waitFor(() => expect(server.context_usage_hidden_providers).toEqual({ plan: true }));
		await syncA.refresh('context_usage_hidden_providers');
		expect(a.current).toEqual({ plan: true });
		syncA.stop();
		syncB.stop();
	});
	it('persists hidden providers separately from expansion and resets on account change', () => {
		const { hiddenProviders, sections, adapters, save } = fixture();
		hiddenProviders.set({ plan: true, 'provider:api': true });
		expect(save).toHaveBeenLastCalledWith('context_usage_hidden_providers', { plan: true, 'provider:api': true });
		expect(fixture().hiddenProviders.current).toEqual({ plan: true, 'provider:api': true });
		expect(sections.current).toEqual({});
		save.mockClear();
		adapters.get('context_usage_hidden_providers')!.apply({ 'provider:other': true, invalid: 'true' });
		expect(hiddenProviders.current).toEqual({ 'provider:other': true });
		adapters.get('context_usage_hidden_providers')!.apply(undefined);
		expect(hiddenProviders.current).toEqual({});
		expect(JSON.parse(localStorage.getItem('ai-shuffle:context_usage_hidden_providers')!)).toEqual({});
		expect(save).not.toHaveBeenCalled();
	});
	it('migrates all legacy sections before any component or provider catalog mounts', () => {
		localStorage.setItem('aihub.contextBreakdownOpen', '0');
		localStorage.setItem('aihub.usageSectionOpen:plan', '0');
		localStorage.setItem('aihub.usageSectionOpen:provider:not-loaded-yet', '0');
		localStorage.setItem('sidebar-collapsed-groups', '{"active":true,"settled":false}');
		const { breakdown, sections, groups, adapters } = fixture();
		expect(breakdown.current).toBe(false);
		expect(sections.current).toEqual({ plan: false, 'provider:not-loaded-yet': false });
		expect(groups.current).toEqual({ active: true, settled: false });
		expect([...adapters.keys()]).toEqual(['context_breakdown_open', 'context_usage_sections', 'sidebar_collapsed_groups', 'context_usage_hidden_providers']);
	});
	it('applies remote state without echo, removes stale cache and resets for another account', () => {
		const { breakdown, sections, groups, adapters, save } = fixture();
		sections.set({ plan: false, 'provider:old': false });
		groups.set({ active: true });
		breakdown.set(false);
		save.mockClear();
		adapters.get('context_usage_sections')!.apply({ 'provider:new': false, futureSection: true, malformed: 'false' });
		expect(sections.current).toEqual({ 'provider:new': false, futureSection: true });
		expect(localStorage.getItem('aihub.usageSectionOpen:provider:old')).toBeNull();
		for (const adapter of adapters.values()) adapter.apply(undefined);
		expect(breakdown.current).toBe(true);
		expect(sections.current).toEqual({});
		expect(groups.current).toEqual({});
		expect(localStorage.getItem('aihub.usageSectionOpen:provider:new')).toBeNull();
		expect(JSON.parse(localStorage.getItem('sidebar-collapsed-groups')!)).toEqual({});
		expect(save).not.toHaveBeenCalled();
	});
	it('keeps choices usable with blocked storage and rejects malformed records', () => {
		const storage = { getItem: () => { throw Error('blocked'); }, setItem: () => { throw Error('blocked'); } } as unknown as Storage;
		const adapters = new Map<string, PreferenceAdapter>();
		const save = vi.fn();
		const stores = createExpansionPreferences({ register: (k, a) => adapters.set(k, a), save }, storage);
		stores.breakdown.set(false);
		stores.groups.set({ settled: true });
		expect(stores.breakdown.current).toBe(false);
		expect(save).toHaveBeenLastCalledWith('sidebar_collapsed_groups', { settled: true });
		adapters.get('sidebar_collapsed_groups')!.apply(['active']);
		expect(stores.groups.current).toEqual({});
	});
	it('preserves an edit during asynchronous startup and sends the server revision', async () => {
		const pending = deferred<{ preferences: Record<string, unknown>; versions: Record<string, string> }>();
		const put = vi.fn(async (_key: string, value: unknown) => ({ value, version: 'v2' }));
		const sync = new PreferenceSync({ all: async () => ({}), allWithVersions: () => pending.promise,
			get: async () => ({ value: false, version: 'v2' }), put });
		const { breakdown } = createExpansionPreferences(sync, localStorage);
		sync.start(false, 'account:A');
		const load = sync.reconnect();
		breakdown.set(false);
		pending.resolve({ preferences: { context_breakdown_open: true, context_usage_sections: {}, sidebar_collapsed_groups: {} },
			versions: { context_breakdown_open: 'v1' } });
		await load;
		expect(breakdown.current).toBe(false);
		expect(put).toHaveBeenCalledWith('context_breakdown_open', false, false, 'v1');
	});
	it('ignores a previous account snapshot after resetting and hydrating the next account', async () => {
		const old = deferred<Record<string, unknown>>();
		const all = vi.fn().mockReturnValueOnce(old.promise).mockResolvedValue({
			context_breakdown_open: true, context_usage_sections: { plan: true }, sidebar_collapsed_groups: { settled: false }, context_usage_hidden_providers: {}
		});
		const put = vi.fn();
		const sync = new PreferenceSync({ all, get: async () => null, put });
		const { breakdown, sections, groups } = createExpansionPreferences(sync, localStorage);
		sync.start(false, 'account:A');
		const loadA = sync.reconnect();
		groups.set({ active: true });
		sync.start(true, 'account:B');
		expect(groups.current).toEqual({});
		await sync.reconnect();
		old.resolve({ context_breakdown_open: false, context_usage_sections: { 'provider:A': false }, sidebar_collapsed_groups: { active: true } });
		await loadA;
		expect(breakdown.current).toBe(true);
		expect(sections.current).toEqual({ plan: true });
		expect(groups.current).toEqual({ settled: false });
		expect(put).not.toHaveBeenCalled();
	});
});
