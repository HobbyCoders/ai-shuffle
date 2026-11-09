/**
 * Settings navigation state: current category, the selected item per
 * collection, the tab per item, the phone-stack step, and one-shot targets
 * (an anchor to reveal, a row to expand).
 *
 * External callers use `openSettings(target)`. Inside Settings, components
 * call `settingsNav.go(target)` (links between categories, search results),
 * `selectCategory`, `selectItem` and `setTab`.
 *
 * Stacked (phone) mode: each step deeper pushes a history entry with SvelteKit
 * `pushState`, so Android Back pops one level. SettingsShell feeds
 * `page.state` back in through `followHistory`.
 */
import { get } from 'svelte/store';
import { pushState, replaceState } from '$app/navigation';
import { activeView, layout } from '$lib/stores/layout.svelte';
import { isCategoryId, PAGE_TYPES, type CategoryId, type SettingsTarget, type SettingsView } from './types';

/** What a Settings history entry carries in `page.state`. */
export interface SettingsHistoryState {
	settingsView?: SettingsView;
	settingsDepth?: number;
	settingsCategory?: CategoryId;
	settingsItem?: string;
	/** Entries below this depth belong to an earlier stack (see `staleBelow`). */
	settingsStaleBelow?: number;
}

/** Old `openSettingsTab(tab)` names and where they live now. */
export const LEGACY_TAB_MAP: Readonly<Record<string, SettingsTarget>> = {
	account: { category: 'general' },
	chat: { category: 'general' },
	voice: { category: 'voice' },
	providers: { category: 'providers' },
	license: { category: 'cloud' },
	cloud: { category: 'cloud' },
	api: { category: 'integrations' },
	plugins: { category: 'plugins' },
	system: { category: 'security' },
	logs: { category: 'logs' }
};

/**
 * Rows that moved to another category. Old deep links (encoded targets in a
 * saved layout, links in docs or notifications) still land on the new row.
 */
export const MOVED_ANCHORS: Readonly<Record<string, SettingsTarget>> = {
	'system.license': { category: 'cloud', anchor: 'cloud.account' },
	'system.seats': { category: 'cloud', anchor: 'cloud.remoteWorkers' },
	// No license key any more: the plan follows the signed-in account.
	'system.licenseKey': { category: 'cloud', anchor: 'cloud.account' },
	'system.deactivate': { category: 'cloud', anchor: 'cloud.account' },
	'cloud.key': { category: 'cloud', anchor: 'cloud.account' },
	'cloud.deactivate': { category: 'cloud', anchor: 'cloud.account' },
	'cloud.plan': { category: 'cloud', anchor: 'cloud.account' },
	// Default models media options became remembered generation choices (#379).
	'models.imageAspect': { category: 'models', item: 'gen-image' },
	'models.imageResolution': { category: 'models', item: 'gen-image' },
	'models.videoAspect': { category: 'models', item: 'gen-video' },
	'models.videoDuration': { category: 'models', item: 'gen-video' },
	'models.videoResolution': { category: 'models', item: 'gen-video' }
};

/**
 * Default models items that were removed (#379): the four generation tools
 * became remembered choices; transform tools are pinned from the catalog.
 */
export const MOVED_MODEL_ITEMS: Readonly<Record<string, string>> = {
	t2i: 'gen-image',
	t2v: 'gen-video',
	t23d: 'gen-3d',
	t2a: 'gen-audio',
	edit: 'catalog',
	i2v: 'catalog',
	i23d: 'catalog',
	a2a: 'catalog',
	stt: 'catalog'
};

/** A target whose anchor moved, rewritten to where that row lives now. */
export function redirectTarget(target: SettingsTarget): SettingsTarget {
	const item = target.category === 'models' && target.item ? MOVED_MODEL_ITEMS[target.item] : undefined;
	// Transform tools are pinned from the catalog's fal.ai tab.
	if (item) return { category: 'models', item, ...(item === 'catalog' ? { tab: 'fal' } : {}) };
	const moved = target.anchor ? MOVED_ANCHORS[target.anchor] : undefined;
	if (!moved) return target;
	const open = target.open && MOVED_ANCHORS[target.open]?.anchor;
	return { ...moved, ...(open ? { open } : {}) };
}

const TARGET_KEYS = ['category', 'item', 'tab', 'anchor', 'open'] as const;

/** Serialize a target into the layout store's `itemId` (a query string). */
export function encodeTarget(target: SettingsTarget): string {
	const params = new URLSearchParams();
	for (const key of TARGET_KEYS) {
		const value = target[key];
		if (value) params.set(key, value);
	}
	return params.toString();
}

/** Parse an `itemId`. Accepts encoded targets, bare category ids and legacy tab names. */
export function decodeTarget(raw: string | null | undefined): SettingsTarget | null {
	if (!raw) return null;
	const text = raw.trim();
	if (!text.includes('=')) {
		if (isCategoryId(text)) return { category: text };
		const legacy = LEGACY_TAB_MAP[text];
		return legacy ? { ...legacy } : null;
	}
	const params = new URLSearchParams(text);
	const category = params.get('category');
	if (!isCategoryId(category)) return null;
	const target: SettingsTarget = { category };
	for (const key of ['item', 'tab', 'anchor', 'open'] as const) {
		const value = params.get(key);
		if (value) target[key] = value;
	}
	return redirectTarget(target);
}

const STORAGE_KEY = 'settings.nav';
const DEPTH: Record<SettingsView, number> = { index: 0, page: 1, detail: 2 };

interface Persisted {
	category?: string;
	selected?: Record<string, string>;
	tabs?: Record<string, string>;
	view?: SettingsView;
	/** The history entry Settings last stood on, by number, and what it held. */
	entry?: OwnEntry;
}

interface OwnEntry {
	id: number;
	state: SettingsHistoryState;
}

/**
 * The Settings position an entry holds, or null. Other features spread
 * `page.state` into their own entries, so the markers alone prove nothing. The
 * phone drawer's copy really is this position, but an entry naming a chat
 * belongs to chat history: landing on it opens that chat.
 */
function settingsPosition(state: SettingsHistoryState | null | undefined): SettingsHistoryState | null {
	return state?.settingsView && !isChatEntry(state) ? state : null;
}

/** SvelteKit's own number for a history entry. Unlike its state, this survives a reload. */
const KIT_ENTRY_KEY = 'sveltekit:history';

function currentEntryId(): number | null {
	if (typeof history === 'undefined') return null;
	const id = (history.state as Record<string, unknown> | null)?.[KIT_ENTRY_KEY];
	return typeof id === 'number' ? id : null;
}

function isChatEntry(state: unknown): boolean {
	return !!(state as { chatSession?: string } | null | undefined)?.chatSession;
}

/** How deep a leftover Settings history entry sits; 0 for any other entry. */
function retainedDepth(state: SettingsHistoryState | null | undefined): number {
	const position = settingsPosition(state);
	return position && position.settingsView !== 'index' ? position.settingsDepth ?? 0 : 0;
}

function tabKey(category: CategoryId, item: string): string {
	return `${category}:${item}`;
}

export class SettingsNavigation {
	category = $state<CategoryId>('general');
	selected = $state<Partial<Record<CategoryId, string>>>({});
	tabs = $state<Record<string, string>>({});
	/** Phone-stack step. Only meaningful while `stacked`. */
	view = $state<SettingsView>('page');
	/** True when the shell shows one pane at a time (phone app or ≤600px wide). */
	stacked = $state(false);
	/** Setting id to scroll to and flash once its row renders. */
	pendingAnchor = $state<string | null>(null);
	/** List item to scroll to and flash (search hit on an item with no anchor). */
	pendingItem = $state<string | null>(null);
	/** Row id whose ExpandToggle should open once rendered. */
	pendingOpen = $state<string | null>(null);
	/** Heading that should take focus after a navigation. */
	focusRequest = $state<{ pane: 'page' | 'detail'; n: number } | null>(null);
	/** Bumps when the search box should take focus. */
	searchFocusRequest = $state(0);
	/** Settings history entries from the first one to this one (stacked mode only). */
	depth = 0;
	/**
	 * Entries below this depth were left by an earlier stack, so the pages they
	 * name are not what the reader is on now. Back skips them to the index.
	 */
	private staleBelow = 0;
	/** The reader chose a page here: a reload or a shrink to one pane keeps it (#1043). */
	private hasPlace = false;
	private ownEntry: OwnEntry | null = null;
	/** Mounted SettingsShell count; `openSettings` updates in place when > 0. */
	mounted = 0;
	/** On-screen SettingsShell count: while > 0, Ctrl+K focuses Settings search. */
	private searchClaims = 0;

	/** True while a Settings page on screen owns Ctrl+K (services/search-shortcut.ts). */
	get searchClaimed(): boolean {
		return this.searchClaims > 0;
	}

	/** A SettingsShell came on screen; call the returned function when it leaves. */
	claimSearchShortcut(): () => void {
		this.searchClaims += 1;
		let released = false;
		return () => {
			if (released) return;
			released = true;
			this.searchClaims -= 1;
		};
	}

	constructor(restore = true) {
		if (restore) this.restore();
	}

	/** Item selected in a collection, if any. */
	selectedItem(category: CategoryId = this.category): string | null {
		return this.selected[category] ?? null;
	}

	/** Current tab of an item: the stored one when still offered, else the first. */
	tabOf(category: CategoryId, item: string | null | undefined, tabs: readonly string[]): string | null {
		if (!tabs.length) return null;
		const stored = item ? this.tabs[tabKey(category, item)] : undefined;
		return stored && tabs.includes(stored) ? stored : tabs[0];
	}

	setTab(category: CategoryId, item: string, tab: string): void {
		this.tabs[tabKey(category, item)] = tab;
		this.persist();
	}

	/** Index click: show a category. */
	selectCategory(category: CategoryId): void {
		this.category = category;
		this.hasPlace = true;
		this.pendingAnchor = null;
		this.pendingItem = null;
		this.pendingOpen = null;
		this.moveTo('page');
		this.requestFocus('page');
		this.persist();
	}

	/** List click: select an item in the current (or given) collection. */
	selectItem(item: string, category: CategoryId = this.category): void {
		this.category = category;
		this.hasPlace = true;
		this.selected[category] = item;
		this.moveTo('detail');
		if (this.stacked) this.requestFocus('detail');
		this.persist();
	}

	/** Clear a collection's selection (e.g. after deleting the selected item). */
	clearSelection(category: CategoryId = this.category): void {
		delete this.selected[category];
		if (this.stacked && this.view === 'detail' && category === this.category) this.view = 'page';
		this.persist();
	}

	/** Jump to a target: deep links, search results, cross-category links. */
	go(requested: SettingsTarget): void {
		const target = redirectTarget(requested);
		const { category } = target;
		this.category = category;
		this.hasPlace = true;
		if (target.item) {
			this.selected[category] = target.item;
			if (target.tab) this.tabs[tabKey(category, target.item)] = target.tab;
		}
		this.pendingAnchor = target.anchor ?? null;
		this.pendingItem = !target.anchor && target.item ? target.item : null;
		this.pendingOpen = target.open ?? null;
		const view: SettingsView = PAGE_TYPES[category] === 'collection' && target.item ? 'detail' : 'page';
		if (this.stacked && this.view === 'index' && view === 'detail') this.moveTo('page');
		this.moveTo(view);
		if (!target.anchor) this.requestFocus(view === 'detail' ? 'detail' : 'page');
		this.persist();
	}

	/** In-UI back button (stacked mode). Pops the history entry it pushed. */
	back(): void {
		if (this.stacked && this.depth > 0 && typeof history !== 'undefined') {
			history.back();
			return;
		}
		this.view = this.view === 'detail' ? 'page' : 'index';
		this.notePlace();
	}

	/** Return to the index (stacked mode), e.g. before focusing search. */
	showIndex(): void {
		if (!this.stacked || this.view === 'index') return;
		if (this.depth > 0 && typeof history !== 'undefined') {
			history.go(-this.depth);
			return;
		}
		this.view = 'index';
		this.notePlace();
	}

	/**
	 * Called by SettingsShell whenever `page.state` changes (Back/Forward).
	 * `onScreen` is false while Settings is kept mounted behind another view: a
	 * chat opened there says nothing about this stack, so the page stays open.
	 */
	followHistory(state: SettingsHistoryState | null | undefined, onScreen = true): void {
		if (!this.stacked) return;
		if (!onScreen && isChatEntry(state)) {
			this.depth = 0;
			this.staleBelow = 0;
			this.noteEntry(null);
			return;
		}
		const position = settingsPosition(state);
		this.noteEntry(position);
		const view = position?.settingsView ?? 'index';
		this.depth = position?.settingsDepth ?? 0;
		if (this.depth === 0) this.staleBelow = 0;
		if (this.depth > 0 && this.depth < this.staleBelow) {
			this.view = 'index';
			this.notePlace();
			if (typeof history !== 'undefined') history.go(-this.depth);
			return;
		}
		if (position?.settingsCategory && isCategoryId(position.settingsCategory)) {
			this.category = position.settingsCategory;
			if (position.settingsItem) this.selected[position.settingsCategory] = position.settingsItem;
		}
		this.view = view;
		this.notePlace();
	}

	/**
	 * Put the stack at its start. `state` is the history entry the shell is
	 * sitting on: when it is left over from an earlier stack, new steps continue
	 * above it and Back skips it.
	 */
	resetStack(view: SettingsView = 'index', state?: SettingsHistoryState | null): void {
		this.view = view;
		this.depth = retainedDepth(state);
		this.staleBelow = this.depth ? this.depth + 1 : 0;
	}

	/**
	 * The shell just became one pane; every narrowing comes through here. Open
	 * the page the reader is on, else the index. `state` is the entry the shell is
	 * sitting on. If it shows this very page (a reload, a resize that changed
	 * nothing) its Back steps are kept. Otherwise it is history, and history is
	 * only ever added to: the stack continues above it and Back skips the leftovers.
	 */
	startStack(state?: SettingsHistoryState | null): void {
		const place: SettingsView = this.hasPlace && this.view !== 'index' ? this.view : 'index';
		const written = settingsPosition(state);
		const position = written ?? this.positionLostToReload(state);
		// The one rewrite Settings makes: its own entry, proved by number, so
		// Back and Forward can land on it again.
		if (position && !written) this.restoreEntry(position);
		if (place !== 'index' && this.entryShows(position, place)) {
			this.staleBelow = position?.settingsStaleBelow ?? 0;
			this.followHistory(position);
			return;
		}
		this.resetStack('index', position);
		if (place === 'index') return;
		this.moveTo('page');
		if (place === 'detail' && this.selected[this.category]) this.moveTo('detail');
	}

	/**
	 * SvelteKit empties the current entry's state when a document loads, so a
	 * reloaded entry cannot say what it was. Its number can: only an entry
	 * Settings itself pushed or stood on is ever remembered here, which is why a
	 * chat entry carrying copied markers can never be mistaken for one.
	 */
	private positionLostToReload(state: SettingsHistoryState | null | undefined): SettingsHistoryState | null {
		if (state && Object.keys(state).length > 0) return null;
		const id = currentEntryId();
		return id !== null && this.ownEntry?.id === id ? this.ownEntry.state : null;
	}

	private restoreEntry(position: SettingsHistoryState): void {
		try {
			replaceState('', position as App.PageState);
		} catch {
			// Router not ready: this entry stays empty until the next visit.
		}
	}

	/** Remember the entry just landed on, or that it is not one of ours. */
	private noteEntry(position: SettingsHistoryState | null): void {
		const id = position ? currentEntryId() : null;
		this.ownEntry = position && id !== null ? { id, state: position } : null;
		this.persist();
	}

	private entryShows(position: SettingsHistoryState | null, place: SettingsView): boolean {
		return position?.settingsView === place
			&& position.settingsCategory === this.category
			&& (place !== 'detail' || position.settingsItem === this.selected[this.category]);
	}

	/** Settings closed: the next visit starts at the index again. */
	forgetPlace(): void {
		this.hasPlace = false;
		this.persist();
	}

	consumeAnchor(): string | null {
		const anchor = this.pendingAnchor;
		this.pendingAnchor = null;
		return anchor;
	}

	consumeItem(): string | null {
		const item = this.pendingItem;
		this.pendingItem = null;
		return item;
	}

	/** True once for the row whose expansion a target asked to open. */
	consumeOpen(anchor: string): boolean {
		if (this.pendingOpen !== anchor) return false;
		this.pendingOpen = null;
		return true;
	}

	/** Back to a fresh state (tests; never needed in the app). */
	reset(): void {
		this.ownEntry = null;
		this.category = 'general';
		this.selected = {};
		this.tabs = {};
		this.view = 'page';
		this.stacked = false;
		this.pendingAnchor = null;
		this.pendingItem = null;
		this.pendingOpen = null;
		this.focusRequest = null;
		this.searchFocusRequest = 0;
		this.depth = 0;
		this.staleBelow = 0;
		this.hasPlace = false;
	}

	requestSearchFocus(): void {
		this.showIndex();
		this.searchFocusRequest += 1;
	}

	private requestFocus(pane: 'page' | 'detail'): void {
		this.focusRequest = { pane, n: (this.focusRequest?.n ?? 0) + 1 };
	}

	private moveTo(view: SettingsView): void {
		let pushed: SettingsHistoryState | null = null;
		if (this.stacked && view !== 'index') {
			// Every step into a page or detail is its own Back step, even
			// sideways (detail to another detail via a link).
			const depth = this.depth + 1;
			const state: SettingsHistoryState = {
				settingsView: view,
				settingsDepth: depth,
				settingsCategory: this.category,
				...(view === 'detail' && this.selected[this.category] ? { settingsItem: this.selected[this.category] } : {}),
				...(this.staleBelow ? { settingsStaleBelow: this.staleBelow } : {})
			};
			try {
				pushState('', state as App.PageState);
				this.depth = depth;
				pushed = state;
			} catch {
				// Router not ready (tests, early boot): the in-UI back still works.
			}
		}
		this.view = view;
		if (pushed) this.noteEntry(pushed);
	}

	private notePlace(): void {
		this.hasPlace = this.view !== 'index';
		this.persist();
	}

	private persist(): void {
		try {
			const data: Persisted = {
				category: this.category, selected: { ...this.selected }, tabs: { ...this.tabs },
				...(this.hasPlace ? { view: this.view } : {}),
				...(this.ownEntry ? { entry: this.ownEntry } : {})
			};
			sessionStorage.setItem(STORAGE_KEY, JSON.stringify(data));
		} catch {
			// Storage blocked: last place is not remembered.
		}
	}

	private restore(): void {
		try {
			const raw = sessionStorage.getItem(STORAGE_KEY);
			if (!raw) return;
			const data = JSON.parse(raw) as Persisted;
			if (isCategoryId(data.category)) this.category = data.category;
			if (data.view === 'page' || data.view === 'detail') {
				this.view = data.view;
				this.hasPlace = isCategoryId(data.category);
			}
			const selected: Partial<Record<CategoryId, string>> = {};
			for (const [k, v] of Object.entries(data.selected ?? {})) {
				if (isCategoryId(k) && typeof v === 'string') selected[k] = v;
			}
			this.selected = selected;
			const tabs: Record<string, string> = {};
			for (const [k, v] of Object.entries(data.tabs ?? {})) if (typeof v === 'string') tabs[k] = v;
			this.tabs = tabs;
			const entry = data.entry;
			if (typeof entry?.id === 'number' && typeof entry.state?.settingsView === 'string') this.ownEntry = entry;
		} catch {
			// Missing or malformed: start at General.
		}
	}
}

export const settingsNav = new SettingsNavigation();

/**
 * Open Settings at a target from anywhere in the app. When Settings is already
 * on screen the store moves in place; otherwise the layout opens the Settings
 * page with the target encoded in its one-shot `itemId`.
 */
export function openSettings(target: SettingsTarget): void {
	const onScreen = settingsNav.mounted > 0 && get(activeView)?.type === 'settings';
	if (onScreen) {
		settingsNav.go(target);
		return;
	}
	layout.openFeaturePage('settings', encodeTarget(target));
}

/** Legacy tab names (`license`, `api`...) mapped onto `openSettings`. Unknown names are ignored. */
export function openSettingsForLegacyTab(tab: string): boolean {
	const target = LEGACY_TAB_MAP[tab];
	if (!target) return false;
	openSettings({ ...target });
	return true;
}
