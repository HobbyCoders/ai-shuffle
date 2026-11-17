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
import { shellHistory, type SettingsPlace } from '$lib/services/app-history.svelte';
import { activeView, layout } from '$lib/stores/layout.svelte';
import { isCategoryId, PAGE_TYPES, type CategoryId, type SettingsTarget, type SettingsView } from './types';

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
interface Persisted { category?: string; selected?: Record<string, string>; tabs?: Record<string, string> }

function tabKey(category: CategoryId, item: string): string {
	return `${category}:${item}`;
}

export class SettingsNavigation {
	category = $state<CategoryId>('general');
	selected = $state<Partial<Record<CategoryId, string>>>({});
	tabs = $state<Record<string, string>>({});
	/** Phone-stack step. Only meaningful while `stacked`. */
	view = $state<SettingsView>('index');
	/** True when the shell shows one pane at a time (phone app or ≤600px wide). */
	stacked = $state(false);
	/**
	 * Collections whose open item replaces the list outside stacked mode
	 * (`replaceOnSelect`, or a page too narrow for both). CollectionPage keeps
	 * it current while mounted; `showList` reads it.
	 */
	onePane = $state<Partial<Record<CategoryId, boolean>>>({});
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
	depth = 0;
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
		shellHistory.detail('settings', { settings: { ...shellHistory.place?.settings, ...this.position(this.view) } }, true);
		this.persist();
	}

	/** Index click: show a category. */
	selectCategory(category: CategoryId): void {
		this.category = category;
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
		if (this.depth > 0 && typeof history !== 'undefined') {
			history.back();
			return;
		}
		this.view = this.view === 'detail' ? 'page' : 'index';
		shellHistory.detail('settings', { settings: this.position(this.view) }, true);
		this.persist();
	}

	/** Show the changed list without leaving a duplicate list entry behind. */
	showList(category: CategoryId = this.category): void {
		if (!this.stacked && !this.onePane[category]) return; // The split already shows the list.
		const here = shellHistory.place?.settings;
		if (here?.category === category && here.listDepth !== undefined) {
			history.go(here.listDepth - here.depth);
			return;
		}
		// A retained or directly linked detail might not have a list to pop to.
		this.clearSelection(category);
		shellHistory.detail('settings', { settings: this.position('page') }, true);
	}

	/** Return to the index (stacked mode), e.g. before focusing search. */
	showIndex(): void {
		if (!this.stacked || this.view === 'index') return;
		if (this.depth > 0 && typeof history !== 'undefined') {
			history.go(-this.depth);
			return;
		}
		this.view = 'index';
		shellHistory.detail('settings', { settings: this.position('index') }, true);
		this.persist();
	}

	/** Repaint the entry without pushing another step or firing an anchor twice. */
	follow(place: SettingsPlace): void {
		this.category = place.category;
		this.view = place.view;
		this.depth = place.depth;
		if (place.item) this.selected[place.category] = place.item;
		else delete this.selected[place.category];
		if (place.item && place.tab) this.tabs[tabKey(place.category, place.item)] = place.tab;
	}

	position(view: SettingsView, depth = this.depth): SettingsPlace {
		const item = this.selectedItem();
		return { category: this.category, view, depth, item,
			tab: item ? this.tabs[tabKey(this.category, item)] ?? null : null };
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
		this.category = 'general';
		this.selected = {};
		this.tabs = {};
		this.view = 'index';
		this.stacked = false;
		this.onePane = {};
		this.pendingAnchor = null;
		this.pendingItem = null;
		this.pendingOpen = null;
		this.focusRequest = null;
		this.searchFocusRequest = 0;
		this.depth = 0;
	}

	requestSearchFocus(): void {
		this.showIndex();
		this.searchFocusRequest += 1;
	}

	private requestFocus(pane: 'page' | 'detail'): void {
		this.focusRequest = { pane, n: (this.focusRequest?.n ?? 0) + 1 };
	}

	private moveTo(view: SettingsView): void {
		const previous = shellHistory.place?.settings;
		const next = this.position(view, previous?.depth ?? 0);
		if (view === 'detail' && previous?.category === next.category) {
			if (previous.view === 'page' && !previous.item) next.listDepth = previous.depth;
			else if (previous.listDepth !== undefined) next.listDepth = previous.listDepth;
		}
		this.view = view;
		if (previous && JSON.stringify(previous) === JSON.stringify(next)) return;
		next.depth++;
		this.depth = next.depth;
		shellHistory.detail('settings', { settings: next });
	}

	private persist(): void {
		try {
			const data: Persisted = {
				category: this.category, selected: { ...this.selected }, tabs: { ...this.tabs },
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
			const selected: Partial<Record<CategoryId, string>> = {};
			for (const [k, v] of Object.entries(data.selected ?? {})) {
				if (isCategoryId(k) && typeof v === 'string') selected[k] = v;
			}
			this.selected = selected;
			const tabs: Record<string, string> = {};
			for (const [k, v] of Object.entries(data.tabs ?? {})) if (typeof v === 'string') tabs[k] = v;
			this.tabs = tabs;
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
