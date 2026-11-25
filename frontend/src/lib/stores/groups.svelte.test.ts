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

	it('deleting a project group saves the cleared assignments through preference sync, as many as the confirmation counts', async () => {
		const { groupMemberCount } = await import('$lib/components/deck/cards/project-groups');
		const { groups } = await load();
		// 'on-peer' is a project only another worker lists; the synced value holds it too.
		for (const id of ['here', 'on-peer']) groups.assignToGroup('projects', id, 'Work');
		groups.assignToGroup('projects', 'kept', 'Home');
		const before = groups.current.projects.assignments;
		const counted = groupMemberCount(before, 'Work');

		groups.deleteGroup('projects', 'Work');

		const saved = sync.save.mock.lastCall!;
		expect(saved[0]).toBe('groups');
		expect(saved[1].projects).toEqual({ groups: [{ name: 'Home', collapsed: true }], assignments: { kept: 'Home' } });
		expect(JSON.parse(localStorage.getItem(STORAGE_KEY)!).projects.assignments).toEqual({ kept: 'Home' });
		expect(Object.keys(before).length - Object.keys(saved[1].projects.assignments).length).toBe(counted);
		expect(counted).toBe(2);
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

	it('finds the group saved on any of an item\'s ids', async () => {
		const { organizeByGroups } = await load();
		const state = {
			projects: { groups: [{ name: 'Work', collapsed: false }], assignments: { clone: 'Work', stale: 'Gone' } },
			profiles: { groups: [], assignments: {} },
			subagents: { groups: [], assignments: {} }
		};
		// A picker row whose group sits on a folded clone, or behind a deleted group.
		const items = [{ id: 'row', aliases: ['clone'] }, { id: 'other', aliases: ['stale', 'clone'] }, { id: 'loose', aliases: [] }];
		const { grouped, ungrouped } = organizeByGroups(items, 'projects', state, (i) => [i.id, ...i.aliases]);
		expect(grouped.get('Work')?.map((i) => i.id)).toEqual(['row', 'other']);
		expect(ungrouped.map((i) => i.id)).toEqual(['loose']);
	});
});

describe('projectSelectGroups', () => {
	const state = (assignments: Record<string, string>, names: string[] = ['Work', 'Empty']) => ({
		projects: { groups: names.map((name) => ({ name, collapsed: false })), assignments },
		profiles: { groups: [], assignments: {} },
		subagents: { groups: [], assignments: {} }
	});
	const projects = [{ id: 'p1', name: 'Website' }, { id: 'p2', name: 'API' }, { id: 'p3', name: 'Docs' }];
	const none = { value: '', label: 'None' };

	it('splits a select into the leading options, one section per used group, then the rest', async () => {
		const { projectSelectGroups } = await load();
		expect(projectSelectGroups(projects, state({ p1: 'Work', p3: 'Work' }), [none])).toEqual([
			{ label: '', options: [none] },
			{ label: 'Work', options: [{ value: 'p1', label: 'Website' }, { value: 'p3', label: 'Docs' }] },
			{ label: 'Other projects', options: [{ value: 'p2', label: 'API' }] }
		]);
	});

	it('stays one flat list when no group holds a listed project', async () => {
		const { projectSelectGroups } = await load();
		expect(projectSelectGroups(projects, state({ elsewhere: 'Work' }), [none])).toEqual([
			{ label: '', options: [none, { value: 'p1', label: 'Website' }, { value: 'p2', label: 'API' }, { value: 'p3', label: 'Docs' }] }
		]);
	});

	it('leaves out the trailing section when every project is grouped', async () => {
		const { projectSelectGroups } = await load();
		const result = projectSelectGroups(projects, state({ p1: 'Work', p2: 'Work', p3: 'Work' }));
		expect(result.map((g) => g.label)).toEqual(['Work']);
	});
});
