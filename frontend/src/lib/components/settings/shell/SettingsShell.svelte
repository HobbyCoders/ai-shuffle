<script lang="ts" module>
	/** Shell width at or below which panes stack. */
	export const STACK_WIDTH = 600;
	/** Shell width at or below which the index narrows and the list is fixed. */
	export const MID_WIDTH = 900;
</script>

<script lang="ts">
	/**
	 * SettingsShell: the Settings page. A 224px index (title, search, nine
	 * categories) beside the content: a PreferencePage, or a CollectionPage
	 * loaded on first visit.
	 *
	 * - Deep links arrive as `initialItemId` (an encoded SettingsTarget from
	 *   `openSettings`); the shell applies it and clears the layout's one-shot
	 *   item id.
	 * - Ctrl+K focuses search while Settings is mounted and on screen (it
	 *   shadows Spotlight and gives it back on unmount); "/" does the same when
	 *   focus is in Settings and not in a field.
	 * - Stacked mode (phone app, or 600px or narrower) shows one pane at a time;
	 *   each step pushes a history entry so Android Back pops one level.
	 * - Read-only (unapproved device): banner on top, controls disabled.
	 */
	import { getContext, onDestroy, tick, untrack } from 'svelte';
	import { page } from '$app/state';
	import { layout } from '$lib/stores/layout.svelte';
	import { platform } from '$lib/stores/platform.svelte';
	import { registerShortcut } from '$lib/services/keyboard';
	import { FEATURE_PANEL_CONTEXT, type FeaturePanelContext } from '$lib/components/layout/feature-panel-context';
	import { settingsNav, decodeTarget, type SettingsHistoryState } from '$lib/settings/navigation.svelte';
	import { setSettingsAccess, settingsAccess } from '$lib/settings/access.svelte';
	import { saveStatus } from '$lib/settings/save-status.svelte';
	import {
		PREFERENCE_PAGES,
		isPreferenceCategory,
		loadCollection,
		loadedCollection,
		type CollectionCategory
	} from '../pages/index';
	import SettingsIndex from './SettingsIndex.svelte';
	import ApprovalBanner from './ApprovalBanner.svelte';
	import './settings.css';

	interface Props {
		/** One-shot deep link from the layout store (`encodeTarget`). */
		initialItemId?: string | null;
	}

	let { initialItemId = null }: Props = $props();

	setSettingsAccess(settingsAccess);
	const panel = getContext<FeaturePanelContext | undefined>(FEATURE_PANEL_CONTEXT);
	const active = $derived(panel?.active ?? true);

	let root: HTMLDivElement | undefined = $state();
	let width = $state(0);
	const stacked = $derived(platform.isMobile || (width > 0 && width <= STACK_WIDTH));
	const mid = $derived(!stacked && width > STACK_WIDTH && width <= MID_WIDTH);
	const category = $derived(settingsNav.category);

	settingsNav.mounted += 1;
	// A stale target from an earlier visit must not fire now.
	if (!untrack(() => initialItemId)) {
		settingsNav.pendingAnchor = null;
		settingsNav.pendingItem = null;
		settingsNav.pendingOpen = null;
	}
	let seenFocus = settingsNav.focusRequest?.n ?? 0;
	onDestroy(() => {
		settingsNav.mounted -= 1;
		saveStatus.reset();
	});

	// Container width drives stacked and mid layouts (narrow windows, split panes).
	$effect(() => {
		if (!root) return;
		width = root.getBoundingClientRect().width;
		if (typeof ResizeObserver === 'undefined') return;
		const ro = new ResizeObserver((entries) => {
			width = entries[0]?.contentRect.width ?? width;
		});
		ro.observe(root);
		return () => ro.disconnect();
	});

	let stackStarted = false;
	$effect(() => {
		const s = stacked;
		untrack(() => {
			settingsNav.stacked = s;
			if (s && !stackStarted) {
				// Entering the stack: start at the index unless a target is landing.
				stackStarted = true;
				if (!initialItemId) settingsNav.resetStack('index');
			} else if (!s) {
				stackStarted = false;
				settingsNav.depth = 0;
			}
		});
	});

	// Deep links (openSettings / legacy openSettingsTab).
	$effect(() => {
		const raw = initialItemId;
		if (!raw) return;
		untrack(() => {
			const target = decodeTarget(raw);
			settingsNav.stacked = stacked;
			if (target) settingsNav.go(target);
			layout.clearFeatureItemId();
		});
	});

	// Android Back / browser Back while stacked.
	let firstHistory = true;
	$effect(() => {
		const state = page.state as SettingsHistoryState;
		const view = state?.settingsView;
		const depth = state?.settingsDepth;
		void view;
		void depth;
		if (firstHistory) {
			firstHistory = false;
			return;
		}
		untrack(() => settingsNav.followHistory(state));
	});

	// Ctrl+K while Settings is on screen.
	$effect(() => {
		if (!active) return;
		const reg = registerShortcut({
			id: 'settings-search',
			description: 'Search settings',
			key: 'k',
			cmdOrCtrl: true,
			category: 'navigation',
			allowInInput: true,
			action: () => settingsNav.requestSearchFocus()
		});
		return () => reg.unregister();
	});

	function onkeydown(event: KeyboardEvent) {
		if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey || event.defaultPrevented) return;
		const t = event.target as HTMLElement | null;
		if (t && (t.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]') || t.isContentEditable))
			return;
		event.preventDefault();
		settingsNav.requestSearchFocus();
	}

	// Reveal a search/deep-link target once its row renders: scroll, flash, open.
	const reducedMotion = () =>
		typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches;

	function flash(el: Element) {
		el.classList.remove('st-flash');
		void (el as HTMLElement).offsetWidth;
		el.classList.add('st-flash');
		setTimeout(() => el.classList.remove('st-flash'), 2300);
	}

	async function reveal(selector: string, block: ScrollLogicalPosition, tries = 40): Promise<void> {
		await tick();
		const el = root?.querySelector(selector);
		if (el) {
			el.scrollIntoView?.({ block, behavior: reducedMotion() ? 'auto' : 'smooth' });
			flash(el);
			return;
		}
		if (tries > 0) setTimeout(() => void reveal(selector, block, tries - 1), 50);
	}

	$effect(() => {
		const anchor = settingsNav.pendingAnchor;
		if (!anchor) return;
		untrack(() => {
			settingsNav.consumeAnchor();
			void reveal(`[data-setting-anchor="${cssEscape(anchor)}"]`, 'center');
		});
	});

	$effect(() => {
		const item = settingsNav.pendingItem;
		if (!item) return;
		untrack(() => {
			settingsNav.consumeItem();
			void reveal(`[data-settings-item="${cssEscape(item)}"]`, 'nearest');
		});
	});

	// Focus the page or detail heading after a navigation.
	$effect(() => {
		const req = settingsNav.focusRequest;
		if (!req || req.n <= seenFocus) return;
		seenFocus = req.n;
		untrack(() => void focusHeading(req.pane, req.n));
	});

	async function focusHeading(pane: 'page' | 'detail', n: number, tries = 40): Promise<void> {
		await tick();
		if (settingsNav.focusRequest?.n !== n) return;
		const el = root?.querySelector<HTMLElement>(`[data-settings-heading="${pane}"]`);
		if (el) {
			el.focus({ preventScroll: true });
			return;
		}
		// A collection may still be loading.
		if (tries > 0) setTimeout(() => void focusHeading(pane, n, tries - 1), 50);
	}

	function cssEscape(value: string): string {
		return typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(value) : value.replace(/["\\]/g, '\\$&');
	}

	const PreferenceComponent = $derived(isPreferenceCategory(category) ? PREFERENCE_PAGES[category] : null);
	const collectionCategory = $derived(isPreferenceCategory(category) ? null : (category as CollectionCategory));
	const ReadyCollection = $derived(collectionCategory ? loadedCollection(collectionCategory) : undefined);
</script>

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div
	class="settings-shell"
	bind:this={root}
	data-stacked={stacked ? '' : undefined}
	data-mid={mid ? '' : undefined}
	data-view={stacked ? settingsNav.view : undefined}
	{onkeydown}
>
	<SettingsIndex />
	<div class="st-content">
		{#if settingsAccess.readOnly}<ApprovalBanner />{/if}
		{#if PreferenceComponent}
			<PreferenceComponent />
		{:else if ReadyCollection}
			<ReadyCollection />
		{:else if collectionCategory}
			{#await loadCollection(collectionCategory)}
				<div class="st-loading" aria-busy="true"></div>
			{:then Collection}
				<Collection />
			{:catch}
				<p class="st-empty" role="alert">Could not load this page. Reload to try again.</p>
			{/await}
		{/if}
	</div>
</div>

<style>
	.st-loading {
		flex: 1;
	}
</style>
