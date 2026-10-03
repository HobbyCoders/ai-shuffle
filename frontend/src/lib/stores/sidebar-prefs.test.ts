/**
 * Tests for sidebar-prefs — the customizable Workspace nav's persistence.
 *
 * The store reads localStorage once at module scope, so anything about LOADING
 * seeds storage and re-imports fresh (vi.resetModules). Anything about editing
 * can reuse one import and reset between cases.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
	DEFAULT_WORKSPACE_SHOWN,
	WORKSPACE_ITEM_IDS,
	WORKSPACE_PREFS_KEY,
	WORKSPACE_PREFS_VERSION,
	normalizeWorkspacePrefs,
	orderedWorkspaceItems,
	sidebarWorkspace,
	splitWorkspaceItems,
	type WorkspaceItemId
} from './sidebar-prefs.svelte';

const ALL: { id: WorkspaceItemId }[] = WORKSPACE_ITEM_IDS.map((id) => ({ id }));
/** What a non-admin is allowed to see (Automations / Settings are gated out). */
const NON_ADMIN = ALL.filter((i) => i.id !== 'automations' && i.id !== 'settings');

function stored(): { version: number; order: string[]; shown: string[] } {
	return JSON.parse(localStorage.getItem(WORKSPACE_PREFS_KEY) ?? '{}');
}

beforeEach(() => {
	localStorage.clear();
	sidebarWorkspace.reset();
});

describe('sidebar-prefs defaults', () => {
	it('starts with every destination in canonical order and five shown', () => {
		expect(sidebarWorkspace.current.order).toEqual([...WORKSPACE_ITEM_IDS]);
		expect(sidebarWorkspace.current.shown).toEqual([...DEFAULT_WORKSPACE_SHOWN]);
	});

	it('splits the shown rows from the ones that fold into More', () => {
		const { shown, hidden } = splitWorkspaceItems(ALL, sidebarWorkspace.current);
		expect(shown.map((i) => i.id)).toEqual(['file-browser', 'notes', 'build', 'project', 'prs']);
		expect(hidden.map((i) => i.id)).toEqual(['automations', 'stats', 'docs', 'settings']);
	});

	it('never surfaces a destination the caller did not offer (admin gating)', () => {
		// Gating happens above the store: a non-admin simply passes fewer items,
		// so no stored preference can put Settings on their panel.
		const { shown, hidden } = splitWorkspaceItems(NON_ADMIN, sidebarWorkspace.current);
		const labels = [...shown, ...hidden].map((i) => i.id);
		expect(labels).not.toContain('automations');
		expect(labels).not.toContain('settings');
		// Customize lists the same set, in nav order rather than shown-then-hidden.
		expect(orderedWorkspaceItems(NON_ADMIN).map((i) => i.id)).toEqual([
			'file-browser',
			'notes',
			'build',
			'project',
			'prs',
			'stats',
			'docs',
		]);
	});

	it('keeps an admin-only id in the stored order while the role lapses', () => {
		sidebarWorkspace.setShown('settings', true);
		// The panel a non-admin sees drops it…
		expect(splitWorkspaceItems(NON_ADMIN).shown.map((i) => i.id)).not.toContain('settings');
		// …but the preference is untouched, so it comes back with the role.
		expect(splitWorkspaceItems(ALL).shown.map((i) => i.id)).toContain('settings');
	});
});

describe('sidebar-prefs editing', () => {
	it('hides a shown row and shows a hidden one, keeping list order', () => {
		sidebarWorkspace.toggle('build');
		expect(sidebarWorkspace.current.shown).toEqual(['file-browser', 'notes', 'project', 'prs']);

		sidebarWorkspace.toggle('automations');
		// Restored in ORDER position, not appended.
		expect(sidebarWorkspace.current.shown).toEqual(['file-browser', 'notes', 'automations', 'project', 'prs']);
	});

	it('is a no-op when setShown asks for the state it is already in', () => {
		const before = sidebarWorkspace.current;
		sidebarWorkspace.setShown('file-browser', true);
		expect(sidebarWorkspace.current).toBe(before);
	});

	it('drops a dragged row in front of its target', () => {
		sidebarWorkspace.reorder('docs', 'file-browser');
		expect(sidebarWorkspace.current.order.slice(0, 3)).toEqual(['docs', 'file-browser', 'notes']);
		// Shown follows the new order.
		expect(sidebarWorkspace.current.shown).toEqual(['file-browser', 'notes', 'build', 'project', 'prs']);

		sidebarWorkspace.setShown('docs', true);
		expect(sidebarWorkspace.current.shown).toEqual(['docs', 'file-browser', 'notes', 'build', 'project', 'prs']);
	});

	it('nudges a row with the keyboard and clamps at the ends', () => {
		sidebarWorkspace.move('notes', -1);
		expect(sidebarWorkspace.current.order.slice(0, 2)).toEqual(['notes', 'file-browser']);

		// Already first: moving up again changes nothing.
		const before = sidebarWorkspace.current.order;
		sidebarWorkspace.move('notes', -1);
		expect(sidebarWorkspace.current.order).toEqual(before);
	});

	it('ignores an unknown id from either path', () => {
		const before = sidebarWorkspace.current;
		sidebarWorkspace.setShown('nope' as WorkspaceItemId, true);
		sidebarWorkspace.reorder('nope' as WorkspaceItemId, 'docs');
		sidebarWorkspace.move('nope' as WorkspaceItemId, 1);
		expect(sidebarWorkspace.current).toBe(before);
	});

	it('arranges the listed ids in sequence and leaves unlisted slots alone', () => {
		// A member's Customize lists no Automations; its slot (index 3) must stay put.
		sidebarWorkspace.arrange(['notes', 'file-browser', 'project', 'build', 'prs', 'stats', 'docs']);
		expect(sidebarWorkspace.current.order).toEqual([
			'notes', 'file-browser', 'project', 'automations', 'build', 'prs', 'stats', 'docs', 'settings',
		]);
		// The rail follows the new order.
		expect(sidebarWorkspace.current.shown).toEqual(['notes', 'file-browser', 'project', 'build', 'prs']);
	});

	it('ignores an arrange that changes nothing or names unknown ids', () => {
		const before = sidebarWorkspace.current;
		sidebarWorkspace.arrange(['file-browser', 'notes', 'nope' as WorkspaceItemId]);
		expect(sidebarWorkspace.current).toBe(before);
	});

	it('persists every edit under the versioned key', () => {
		sidebarWorkspace.toggle('stats');
		sidebarWorkspace.move('stats', -2);

		const blob = stored();
		expect(blob.version).toBe(WORKSPACE_PREFS_VERSION);
		expect(blob.shown).toContain('stats');
		expect(blob.order).toEqual(sidebarWorkspace.current.order);
	});

	it('notifies subscribers so the nav re-renders', () => {
		const seen: number[] = [];
		const stop = sidebarWorkspace.subscribe((p) => seen.push(p.shown.length));
		sidebarWorkspace.toggle('docs');
		stop();
		expect(seen).toEqual([5, 6]);
	});
});

describe('sidebar-prefs loading', () => {
	it('reconciles a stored blob: drops retired ids, appends new ones', () => {
		const prefs = normalizeWorkspacePrefs({
			version: WORKSPACE_PREFS_VERSION,
			order: ['docs', 'gone-in-a-later-release', 'file-browser'],
			shown: ['docs', 'gone-in-a-later-release']
		});

		// Stored order first, then everything this release ships that was missing —
		// so a newly-added destination appears without a migration.
		expect(prefs.order.slice(0, 2)).toEqual(['docs', 'file-browser']);
		expect(new Set(prefs.order)).toEqual(new Set(WORKSPACE_ITEM_IDS));
		expect(prefs.shown).toEqual(['docs']);
	});

	it('discards a blob written by another schema version', () => {
		const prefs = normalizeWorkspacePrefs({ version: 99, order: ['docs'], shown: [] });
		expect(prefs.order).toEqual([...WORKSPACE_ITEM_IDS]);
		expect(prefs.shown).toEqual([...DEFAULT_WORKSPACE_SHOWN]);
	});

	it('treats an empty shown list as a real choice, not a missing field', () => {
		const prefs = normalizeWorkspacePrefs({
			version: WORKSPACE_PREFS_VERSION,
			order: [...WORKSPACE_ITEM_IDS],
			shown: []
		});
		expect(prefs.shown).toEqual([]);
	});

	it('restores a persisted blob on a fresh load and retires the old flag', async () => {
		vi.resetModules();
		localStorage.clear();
		localStorage.setItem('ai-shuffle.sidebar.workspaceOpen', '0');
		localStorage.setItem(
			WORKSPACE_PREFS_KEY,
			JSON.stringify({
				version: WORKSPACE_PREFS_VERSION,
				order: ['stats', 'file-browser', 'notes', 'build', 'automations', 'project', 'docs', 'settings'],
				shown: ['stats', 'notes']
			})
		);

		const fresh = (await import('./sidebar-prefs.svelte')).sidebarWorkspace;

		expect(fresh.current.order[0]).toBe('stats');
		expect(fresh.current.shown).toEqual(['stats', 'notes']);
		// The collapsible this feature replaced must not linger in storage.
		expect(localStorage.getItem('ai-shuffle.sidebar.workspaceOpen')).toBeNull();
	});

	it('falls back to defaults on corrupt JSON instead of throwing', async () => {
		vi.resetModules();
		localStorage.clear();
		localStorage.setItem(WORKSPACE_PREFS_KEY, '{not json');

		const fresh = (await import('./sidebar-prefs.svelte')).sidebarWorkspace;

		expect(fresh.current.shown).toEqual([...DEFAULT_WORKSPACE_SHOWN]);
	});
});
