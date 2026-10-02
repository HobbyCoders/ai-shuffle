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
import { pushState } from '$app/navigation';
import { activeView, layout } from '$lib/stores/layout.svelte';
import { isCategoryId, PAGE_TYPES, type CategoryId, type SettingsTarget, type SettingsView } from './types';

/** What a Settings history entry carries in `page.state`. */
export interface SettingsHistoryState {
	settingsView?: SettingsView;
	settingsDepth?: number;
	settingsCategory?: CategoryId;
	settingsItem?: string;
}

/** Old `openSettingsTab(tab)` names and where they live now. */
export const LEGACY_TAB_MAP: Readonly<Record<string, SettingsTarget>> = {
	account: { category: 'general' },
	chat: { category: 'general' },
	voice: { category: 'voice' },
	providers: { category: 'providers' },
	license: { category: 'system', anchor: 'system.license' },
	api: { category: 'integrations' },
	plugins: { category: 'plugins' },
	system: { category: 'security' },
	logs: { category: 'logs' }
};

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
	return target;
}

const STORAGE_KEY = 'settings.nav';
const DEPTH: Record<SettingsView, number> = { index: 0, page: 1, detail: 2 };

interface Persisted {
	category?: string;
	selected?: Record<string, string>;
	tabs?: Record<string, string>;
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
	/** History entries pushed for the current stack (stacked mode only). */
	depth = 0;
	/** Mounted SettingsShell count; `openSettings` updates in place when > 0. */
	mounted = 0;

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
	go(target: SettingsTarget): void {
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
		if (this.stacked && this.depth > 0 && typeof history !== 'undefined') {
			history.back();
			return;
		}
		this.view = this.view === 'detail' ? 'page' : 'index';
	}

	/** Return to the index (stacked mode), e.g. before focusing search. */
	showIndex(): void {
		if (!this.stacked || this.view === 'index') return;
		if (this.depth > 0 && typeof history !== 'undefined') {
			history.go(-this.depth);
			return;
		}
		this.view = 'index';
	}

	/** Called by SettingsShell whenever `page.state` changes (Back/Forward). */
	followHistory(state: SettingsHistoryState | null | undefined): void {
		if (!this.stacked) return;
		const view = state?.settingsView ?? 'index';
		this.depth = state?.settingsDepth ?? 0;
		if (state?.settingsCategory && isCategoryId(state.settingsCategory)) {
			this.category = state.settingsCategory;
			if (state.settingsItem) this.selected[state.settingsCategory] = state.settingsItem;
		}
		this.view = view;
	}

	/** Put the stack at its start (shell mount in stacked mode, no target). */
	resetStack(view: SettingsView = 'index'): void {
		this.view = view;
		this.depth = 0;
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
		this.view = 'page';
		this.stacked = false;
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
		if (this.stacked && view !== 'index') {
			// Every step into a page or detail is its own Back step, even
			// sideways (detail to another detail via a link).
			const depth = this.depth + 1;
			const state: SettingsHistoryState = {
				settingsView: view,
				settingsDepth: depth,
				settingsCategory: this.category,
				...(view === 'detail' && this.selected[this.category] ? { settingsItem: this.selected[this.category] } : {})
			};
			try {
				pushState('', state as App.PageState);
				this.depth = depth;
			} catch {
				// Router not ready (tests, early boot): the in-UI back still works.
			}
		}
		this.view = view;
	}

	private persist(): void {
		try {
			const data: Persisted = { category: this.category, selected: { ...this.selected }, tabs: { ...this.tabs } };
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
