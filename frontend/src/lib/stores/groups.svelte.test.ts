/**
 * Groups store — project/profile/subagent grouping: create/assign/delete
 * transitions, dedupe, persistence (localStorage + preference sync), the
 * server-value sanitizer, and the pure organizeByGroups helper.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

type Binding = { read: () => unknown; apply: (value: unknown) => void };

const sync = vi.hoisted(() => ({
	bindings: new Map<string, Binding>(),
	save: vi.fn(),
	refresh: vi.fn()
}));

vi.mock('$lib/services/preferences', () => ({
	preferenceSync: {
		register: (key: string, binding: Binding) => sync.bindings.set(key, binding),
		save: sync.save,
		refresh: sync.refresh
	}
}));

const STORAGE_KEY = 'aihub_groups';

async function load() {
	vi.resetModules();
	sync.bindings.clear();
	return import('./groups.svelte');
}

beforeEach(() => {
	localStorage.clear();
	sync.save.mockReset();
	sync.refresh.mockReset();
});

describe('groups store', () => {
	it('starts empty, or merges a cached value over the defaults', async () => {
		let { groups } = await load();
		expect(groups.current.projects).toEqual({ groups: [], assignments: {} });

		localStorage.setItem(STORAGE_KEY, JSON.stringify({ projects: { groups: [{ name: 'Work', collapsed: false }], assignments: { p1: 'Work' } } }));
		({ groups } = await load());
		expect(groups.current.projects.assignments).toEqual({ p1: 'Work' });
		expect(groups.current.profiles).toEqual({ groups: [], assignments: {} });
	});

	it('falls back to defaults on a corrupt cache', async () => {
		localStorage.setItem(STORAGE_KEY, '{not json');
		const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
		const { groups } = await load();
		expect(groups.current.subagents).toEqual({ groups: [], assignments: {} });
		spy.mockRestore();
	});

	it('creates collapsed groups once per name', async () => {
		const { groups } = await load();
		groups.createGroup('projects', 'Work');
		groups.createGroup('projects', 'Work');
		expect(groups.current.projects.groups).toEqual([{ name: 'Work', collapsed: true }]);
		expect(groups.current.profiles.groups).toEqual([]);
	});

	it('persists every change locally and through preference sync', async () => {
		const { groups } = await load();
		groups.createGroup('profiles', 'Ops');
		expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).profiles.groups).toEqual([{ name: 'Ops', collapsed: true }]);
		expect(sync.save).toHaveBeenLastCalledWith('groups', groups.current);
	});

	it('assigning to a missing group creates it; reassigning moves the item', async () => {
		const { groups } = await load();
		groups.assignToGroup('projects', 'p1', 'A');
		groups.assignToGroup('projects', 'p1', 'B');
		expect(groups.current.projects.groups.map((g) => g.name)).toEqual(['A', 'B']);
		expect(groups.current.projects.assignments).toEqual({ p1: 'B' });
		groups.removeFromGroup('projects', 'p1');
		expect(groups.current.projects.assignments).toEqual({});
		expect(groups.current.projects.groups.map((g) => g.name)).toEqual(['A', 'B']);
	});

	it('deleting a group ungroups only its members', async () => {
		const { groups } = await load();
		groups.assignToGroup('subagents', 'x', 'A');
		groups.assignToGroup('subagents', 'y', 'B');
		groups.deleteGroup('subagents', 'A');
		expect(groups.current.subagents).toEqual({ groups: [{ name: 'B', collapsed: true }], assignments: { y: 'B' } });
	});

	it('reload delegates to preference sync', async () => {
		const { groups } = await load();
		await groups.reload();
		expect(sync.refresh).toHaveBeenCalledWith('groups');
	});

	it('sanitizes a server value before applying it', async () => {
		const { groups } = await load();
		const seen: unknown[] = [];
		groups.subscribe((v) => seen.push(v));
		sync.bindings.get('groups')!.apply({
			projects: {
				groups: [{ name: 'Keep', collapsed: 1 }, null, { name: 42 }],
				assignments: { a: 'Keep', b: 7 }
			},
			profiles: 'garbage'
		});
		expect(groups.current.projects.groups).toEqual([{ name: 'Keep', collapsed: true }]);
		expect(groups.current.projects.assignments).toEqual({ a: 'Keep' });
		expect(groups.current.profiles).toEqual({ groups: [], assignments: {} });
		expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).projects.assignments).toEqual({ a: 'Keep' });
		// Applying a server value must not echo it back as a new save.
		expect(sync.save).not.toHaveBeenCalled();
		expect(seen).toHaveLength(2);
	});

	it('reset restores the defaults', async () => {
		const { groups } = await load();
		groups.assignToGroup('projects', 'p1', 'A');
		groups.reset();
		expect(groups.current.projects).toEqual({ groups: [], assignments: {} });
	});
});

describe('organizeByGroups', () => {
	it('keeps group order, includes empty groups and ungroups orphans', async () => {
		const { organizeByGroups } = await load();
		const state = {
			projects: {
				groups: [{ name: 'B', collapsed: false }, { name: 'A', collapsed: true }, { name: 'Empty', collapsed: true }],
				assignments: { '1': 'A', '2': 'B', '3': 'Deleted' }
			},
			profiles: { groups: [], assignments: {} },
			subagents: { groups: [], assignments: {} }
		};
		const items = [{ id: '1' }, { id: '2' }, { id: '3' }, { id: '4' }];
		const { grouped, ungrouped, groupOrder } = organizeByGroups(items, 'projects', state);
		expect([...grouped.keys()]).toEqual(['B', 'A', 'Empty']);
		expect(grouped.get('A')).toEqual([{ id: '1' }]);
		expect(grouped.get('Empty')).toEqual([]);
		expect(ungrouped.map((i) => i.id)).toEqual(['3', '4']);
		expect(groupOrder.map((g) => g.name)).toEqual(['B', 'A', 'Empty']);
	});
});
