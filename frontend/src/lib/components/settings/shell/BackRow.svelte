<script lang="ts" module>
	/** The visible parent step lends its original callback to either phone bar. */
	export const phoneBack = $state({ owner: null as symbol | null, host: null as symbol | null,
		label: '', onclick: null as (() => void) | null, status: null as import('svelte').Snippet | null, actions: null as import('svelte').Snippet | null });
</script>
<script lang="ts">
	import { getContext, onMount, type Snippet } from 'svelte';
	import { ChevronLeft } from 'lucide-svelte';
	import { platform } from '$lib/stores/platform.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { FEATURE_PANEL_CONTEXT, type FeaturePanelContext } from '../../layout/feature-panel-context';
	let { label, onclick, children, actions }: { label: string; onclick: () => void; children?: Snippet; actions?: Snippet } = $props();
	const feature = getContext<FeaturePanelContext | undefined>(FEATURE_PANEL_CONTEXT);
	const owner = Symbol('phone-back');
	let row: HTMLDivElement | undefined = $state();
	let phone = $state(false);
	let visible = $state(false);
	onMount(() => {
		const parent = row!.parentElement!;
		const query = window.matchMedia('(max-width: 600px)');
		const measure = () => { phone = query.matches; visible = parent.getBoundingClientRect().height > 0 && !parent.closest('[hidden], [inert]'); };
		const ro = new ResizeObserver(measure); ro.observe(parent);
		const pane = parent.closest('[data-view-shell]');
		const mo = new MutationObserver(measure);
		if (pane) mo.observe(pane, { attributes: true, attributeFilter: ['data-pane', 'data-stacked'] });
		query.addEventListener('change', measure); measure();
		return () => { ro.disconnect(); mo.disconnect(); query.removeEventListener('change', measure); };
	});
	$effect(() => {
		// Recheck CSS-swapped Settings panes as well as ViewShell's hidden panes.
		settingsNav.view; feature?.active;
		if (row) visible = row.parentElement!.getBoundingClientRect().height > 0 && !row.closest('[hidden], [inert]');
	});
	$effect(() => {
		if ((!phone && !platform.isMobile) || !visible || feature?.active === false) return;
		phoneBack.owner = owner; phoneBack.label = label; phoneBack.onclick = onclick; phoneBack.status = children ?? null; phoneBack.actions = actions ?? null;
		return () => { if (phoneBack.owner === owner) { phoneBack.owner = null; phoneBack.label = ''; phoneBack.onclick = null; phoneBack.status = null; phoneBack.actions = null; } };
	});
</script>
<div class="st-back-row st-dtop" bind:this={row} data-in-topbar={phoneBack.host && (phone || platform.isMobile) ? '' : undefined}>
	<button type="button" class="st-back" aria-label={`Back to ${label}`} {onclick}><ChevronLeft /><span>{label}</span></button>
	<div class="back-status">{@render children?.()}{#if !phoneBack.host && (phone || platform.isMobile)}{@render actions?.()}{/if}</div>
</div>
<style>
	.st-back-row { position: absolute; top: 10px; inset-inline: var(--st-gutter); display: flex; align-items: center; min-height: 32px; gap: 8px; }
	.back-status { display: flex; align-items: center; margin-left: auto; }
	:global(.settings-shell[data-stacked]) .st-back-row { position: sticky; top: 0; z-index: 5; min-height: 56px; margin: 0 calc(-1 * var(--st-gutter)) 20px; padding: 0 16px 0 8px; background: var(--st-bg); border-bottom: 1px solid var(--st-border); }
	.st-back-row[data-in-topbar] { display: none; }
	:global(.settings-shell[data-stacked]) .st-back-row[data-in-topbar] { display: none; }
</style>
