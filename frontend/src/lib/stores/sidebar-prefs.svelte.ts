import { preferenceSync } from '$lib/services/preferences';
/**
 * Sidebar preferences — the customizable Workspace nav.
 *
 * The sidebar's Workspace section used to be a fixed, collapsible list of every
 * destination. It is now a short list the user curates: three rows are shown by
 * default and everything else folds into a "More" flyout. This store owns the
 * two facts that survive a reload — the ORDER of the destinations and the SET
 * that is shown inline — and nothing else. Which rows are *available* is not a
 * preference: admin-only destinations are filtered by the caller (see
 * `splitWorkspaceItems`), so hiding an item you cannot see is impossible and a
 * demoted admin's stored order stays intact for when the role comes back.
 *
 * Storage is one versioned blob under `ai-shuffle.sidebar.workspace`. The
 * version is a hard gate: a blob written by a different schema is discarded
 * rather than migrated, because the whole payload is two lists of ids that cost
 * nothing to rebuild. Within a version the blob is *reconciled* instead of
 * trusted — unknown ids are dropped and ids added by a newer release are
 * appended — so shipping a new destination makes it appear for existing users
 * without a migration.
 *
 * The retired `ai-shuffle.sidebar.workspaceOpen` collapsible flag is removed on
 * first load so it cannot linger in storage forever.
 *
 * Svelte 5 runes with the usual subscribe() shim, so `$workspacePrefs` works in
 * components (see store-utils.ts and sessionMode.svelte.ts for the pattern).
 */

import { browser } from '$app/environment';
import { createSubscribers } from './store-utils';

/**
 * A Workspace destination. Every id is also a `ViewType`, which is what makes
 * the nav rows able to hand their id straight to `onOpenFeature` /
 * `preloadPanelForView` without a lookup table.
 */
export type WorkspaceItemId =
	| 'file-browser'
	| 'notes'
	| 'build'
	| 'automations'
	| 'project'
	| 'stats'
	| 'docs'
	| 'settings';

/** Canonical order — also the fallback order for a fresh install. */
export const WORKSPACE_ITEM_IDS: readonly WorkspaceItemId[] = [
	'file-browser',
	'notes',
	'build',
	'automations',
	'project',
	'stats',
	'docs',
	'settings'
];

/** Shown inline on a fresh install; the rest start folded into More. */
export const DEFAULT_WORKSPACE_SHOWN: readonly WorkspaceItemId[] = [
	'file-browser',
	'build',
	'project'
];

export const WORKSPACE_PREFS_KEY = 'ai-shuffle.sidebar.workspace';
export const WORKSPACE_PREFS_VERSION = 1;

/** The collapsible this feature replaces. Cleared once, then forgotten. */
const LEGACY_WORKSPACE_OPEN_KEY = 'ai-shuffle.sidebar.workspaceOpen';

export interface WorkspacePrefs {
	version: number;
	/** Every known id exactly once, in the user's chosen order. */
	order: WorkspaceItemId[];
	/** The subset of `order` rendered inline; the rest live in More. */
	shown: WorkspaceItemId[];
}

function defaults(): WorkspacePrefs {
	return {
		version: WORKSPACE_PREFS_VERSION,
		order: [...WORKSPACE_ITEM_IDS],
		shown: [...DEFAULT_WORKSPACE_SHOWN]
	};
}

function isKnown(id: unknown): id is WorkspaceItemId {
	return typeof id === 'string' && (WORKSPACE_ITEM_IDS as readonly string[]).includes(id);
}

/**
 * Rebuild a trustworthy prefs object from whatever storage handed back.
 *
 * Keeps the stored order for ids we still ship, drops ids we no longer do, and
 * appends (in canonical position order) anything a newer release added — so a
 * new destination is visible in Customize the first time it ships instead of
 * silently missing until the user resets.
 */
export function normalizeWorkspacePrefs(raw: unknown): WorkspacePrefs {
	if (!raw || typeof raw !== 'object') return defaults();
	const blob = raw as Partial<WorkspacePrefs>;
	if (blob.version !== WORKSPACE_PREFS_VERSION) return defaults();

	const order: WorkspaceItemId[] = [];
	for (const id of Array.isArray(blob.order) ? blob.order : []) {
		if (isKnown(id) && !order.includes(id)) order.push(id);
	}
	for (const id of WORKSPACE_ITEM_IDS) {
		if (!order.includes(id)) order.push(id);
	}

	// An absent `shown` is a genuinely empty selection, not a missing field:
	// hiding every row is a legitimate (if minimal) choice, so only a non-array
	// falls back to the defaults.
	const shown: WorkspaceItemId[] = Array.isArray(blob.shown)
		? order.filter((id) => (blob.shown as unknown[]).includes(id))
		: [...DEFAULT_WORKSPACE_SHOWN];

	return { version: WORKSPACE_PREFS_VERSION, order, shown };
}

function read(): WorkspacePrefs {
	if (!browser) return defaults();
	try {
		localStorage.removeItem(LEGACY_WORKSPACE_OPEN_KEY);
		const stored = localStorage.getItem(WORKSPACE_PREFS_KEY);
		return normalizeWorkspacePrefs(stored ? JSON.parse(stored) : null);
	} catch {
		// Private mode / blocked storage / corrupt JSON: the nav still works,
		// it just starts from the defaults every load.
		return defaults();
	}
}

function write(prefs: WorkspacePrefs) {
	if (!browser) return;
	try {
		localStorage.setItem(WORKSPACE_PREFS_KEY, JSON.stringify(prefs));
	} catch {
		// Storage full or blocked — the change still applies for this session.
	}
}

let _prefs = $state<WorkspacePrefs>(read());

const { subscribe, notifySubscribers, createDerived } = createSubscribers<WorkspacePrefs>(
	() => _prefs,
	(v) => {
		_prefs = v;
	}
);

function commit(next: WorkspacePrefs) {
 preferenceSync.save('sidebar_workspace', next);
	_prefs = next;
	write(next);
	notifySubscribers();
}

export const sidebarWorkspace = {
	get current(): WorkspacePrefs {
		return _prefs;
	},

	subscribe,

	isShown(id: WorkspaceItemId): boolean {
		return _prefs.shown.includes(id);
	},

	/** Show or hide one destination, preserving its place in the order. */
	setShown(id: WorkspaceItemId, shown: boolean) {
		if (!isKnown(id) || _prefs.shown.includes(id) === shown) return;
		const next = shown
			? _prefs.order.filter((x) => x === id || _prefs.shown.includes(x))
			: _prefs.shown.filter((x) => x !== id);
		commit({ ..._prefs, shown: next });
	},

	toggle(id: WorkspaceItemId) {
		this.setShown(id, !_prefs.shown.includes(id));
	},

	/**
	 * Drag path: lift `id` out and drop it where `beforeId` currently sits.
	 * `shown` is re-derived from the new order so the inline rows follow it.
	 */
	reorder(id: WorkspaceItemId, beforeId: WorkspaceItemId) {
		if (!isKnown(id) || !isKnown(beforeId) || id === beforeId) return;
		const order = _prefs.order.filter((x) => x !== id);
		const at = order.indexOf(beforeId);
		if (at < 0) return;
		order.splice(at, 0, id);
		commit({ ..._prefs, order, shown: order.filter((x) => _prefs.shown.includes(x)) });
	},

	/** Keyboard path: nudge `id` by `delta` places, clamped at the ends. */
	move(id: WorkspaceItemId, delta: number) {
		const from = _prefs.order.indexOf(id);
		if (from < 0 || !delta) return;
		const to = Math.max(0, Math.min(_prefs.order.length - 1, from + delta));
		if (to === from) return;
		const order = [..._prefs.order];
		order.splice(to, 0, ...order.splice(from, 1));
		commit({ ..._prefs, order, shown: order.filter((x) => _prefs.shown.includes(x)) });
	},

	reset() {
		commit(defaults());
	},

	/** Re-read storage. Only useful after storage was cleared underneath us. */
	reload() {
		_prefs = read();
		notifySubscribers();
	}
};

/** `$workspacePrefs` in a component yields the current prefs. */
export const workspacePrefs = createDerived(() => _prefs);

/**
 * Split the destinations the caller is allowed to see into the inline rows and
 * the ones that fold into More, both in the user's order.
 *
 * Admin gating happens before this: the caller passes only the items the
 * current role may open, so a non-admin can never be offered Settings by a
 * stored preference, and an admin's stored order is preserved verbatim while
 * their role lapses.
 */
export function splitWorkspaceItems<T extends { id: WorkspaceItemId }>(
	items: readonly T[],
	prefs: WorkspacePrefs = _prefs
): { shown: T[]; hidden: T[] } {
	const byId = new Map(items.map((item) => [item.id, item]));
	const ordered = prefs.order
		.map((id) => byId.get(id))
		.filter((item): item is T => item !== undefined);
	return {
		shown: ordered.filter((item) => prefs.shown.includes(item.id)),
		hidden: ordered.filter((item) => !prefs.shown.includes(item.id))
	};
}

/** Every allowed destination in the user's order — what Customize lists. */
export function orderedWorkspaceItems<T extends { id: WorkspaceItemId }>(
	items: readonly T[],
	prefs: WorkspacePrefs = _prefs
): T[] {
	const byId = new Map(items.map((item) => [item.id, item]));
	return prefs.order
		.map((id) => byId.get(id))
		.filter((item): item is T => item !== undefined);
}
preferenceSync.register('sidebar_workspace', {
 read: () => _prefs,
 apply(value) { _prefs = normalizeWorkspacePrefs(value); write(_prefs); notifySubscribers(); }
});
