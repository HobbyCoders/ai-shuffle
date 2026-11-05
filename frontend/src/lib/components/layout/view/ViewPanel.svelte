<script lang="ts" module>
	import type { Snippet as ActionSnippet } from 'svelte';
	/** The active native destination lends its original actions to the shell bar. */
	export const phonePanelActions = $state({ owner: null as symbol | null, label: '', visible: false, content: null as ActionSnippet | null });
</script>
<script lang="ts">
	/** Chats' column anatomy, including its own Settings token scope when docked. */
	import { getAllContexts, getContext, onMount, type Snippet } from 'svelte';
	import { FEATURE_PANEL_CONTEXT, type FeaturePanelContext } from '../feature-panel-context';
	import PanelContext from './PanelContext.svelte';
	import { platform } from '$lib/stores/platform.svelte';
	import '../../settings/shell/settings.css';
	import { layout } from '$lib/stores/layout.svelte';
	import PanelToggle from './PanelToggle.svelte';
	import ListEmptyScope from '../../settings/shell/ListEmptyScope.svelte';
	interface Props {
		title: string;
		count?: string | number;
		actions?: Snippet;
		search?: Snippet;
		filters?: Snippet;
		footer?: Snippet;
		children?: Snippet;
		listLabel?: string;
		loading?: boolean;
		onkeydown?: (event: KeyboardEvent) => void;
	}
	let { title, count, actions, search, filters, footer, children, listLabel,
		loading = false, onkeydown }: Props = $props();
	const contexts = getAllContexts();
	const feature = getContext<FeaturePanelContext | undefined>(FEATURE_PANEL_CONTEXT);
	const owner = Symbol('phone-panel-actions');
	let phone = $state(false);
	let element: HTMLElement | undefined = $state();
	let visible = $state(false);
	onMount(() => {
		const query = typeof window.matchMedia === 'function' ? window.matchMedia('(max-width: 600px)') : null;
		const update = () => { phone = !!query?.matches; visible = !!element?.getBoundingClientRect().width && !!element?.getBoundingClientRect().height; };
		// Every supported browser has ResizeObserver; jsdom test renders do not.
		const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
		observer?.observe(element!);
		update(); query?.addEventListener('change', update);
		return () => { observer?.disconnect(); query?.removeEventListener('change', update); };
	});
	$effect(() => {
		if (feature?.active === false) return;
		phonePanelActions.owner = owner;
		phonePanelActions.label = title;
		phonePanelActions.visible = visible;
		phonePanelActions.content = nativeActions;
		return () => {
			if (phonePanelActions.owner === owner) {
				phonePanelActions.owner = null;
				phonePanelActions.label = '';
				phonePanelActions.visible = false;
				phonePanelActions.content = null;
			}
		};
	});
</script>

{#snippet nativeActions()}<PanelContext {contexts}><div class="settings-shell native-panel-actions">{@render actions?.()}</div></PanelContext>{/snippet}
<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<section bind:this={element} class="settings-shell view-panel" class:native={platform.isMobile} aria-label={title} aria-busy={loading || undefined} {onkeydown}>
	<header class="panel-header">
		<h1>{title}</h1>
		{#if count !== undefined}<span class="panel-count">{count}</span>{/if}
		<PanelToggle />
		<div class="panel-actions">{#if !platform.isMobile && !phone}{@render actions?.()}{/if}</div>
	</header>
	{#if search}<div class="panel-search">{@render search()}</div>{/if}
	{#if filters}<div class="panel-filters">{@render filters()}</div>{/if}
	<!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_static_element_interactions -->
	<div onclick={(event) => { if (layout.current?.sidebarOverlay && !platform.isMobile && window.innerWidth >= 768 && (event.target as Element).closest('button:not([aria-expanded]), a')) layout.setSidebarCollapsed(true); }} class="panel-body st-scroll" data-settings-list role="region" aria-label={listLabel ?? `${title} list`}>
		<ListEmptyScope>{@render children?.()}</ListEmptyScope>
	</div>
	{#if footer}<footer class="panel-footer">{@render footer()}</footer>{/if}
</section>

<style>
	.view-panel { display: flex; flex-direction: column; height: 100%; min-height: 0; min-width: 0; background: var(--sidebar); color: var(--sidebar-foreground); container: view-panel / inline-size; }
	.panel-header { display: flex; flex: none; align-items: center; gap: 8px; height: 52px; padding: 0 8px 0 20px; }
	h1 { margin: 0; min-width: 0; font-size: 16px; font-weight: 600; line-height: 1.5; letter-spacing: -0.005em; }
	.panel-count { font-size: 12px; color: var(--st-mfg); font-variant-numeric: tabular-nums; }
	.panel-actions { margin-left: auto; display: flex; align-items: center; gap: 2px; }
	.panel-search { flex: none; padding: 0 8px 8px; }
	.panel-filters { display: flex; flex: none; align-items: center; gap: 8px; flex-wrap: wrap; padding: 4px 8px 8px; }
	.panel-body { flex: 1; min-height: 0; overflow-y: auto; overscroll-behavior: contain; padding: 0 8px 12px; }
	.panel-body:has(:global(.st-empty-detail)) { display: flex; flex-direction: column; }
	.panel-body :global(.st-empty-detail) { min-height: 0; flex: none; justify-content: flex-start; padding: 12px; }
	@media (max-width: 600px) { .panel-body :global(.st-empty-detail) { padding-inline: 0; } }
	.panel-filters:not(:has(*)) { display: none; }
	.view-panel:has(:global(.st-empty-detail)) .panel-search { padding-bottom: 0; }
	.view-panel:has(:global(.st-empty-detail)) .panel-filters { display: none; }
	.panel-footer { flex: none; padding: 8px; }
	/* The real phone shell already names the page in its app bar. Keep actions. */
	.native .panel-header { display: none; }
	.native-panel-actions { display: flex; align-items: center; gap: 2px; container: none; flex: none; width: max-content; min-width: 44px; height: auto; background: none; }
	:global(.view-shell[data-stacked]) .panel-header h1, :global(.view-shell[data-stacked]) .panel-count { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
	.native h1, .native .panel-count { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
	.native .panel-search { padding-top: 12px; }
	@media (max-width: 600px) {
		.view-panel { background: var(--background); }
		.panel-header { display: none; }
		.panel-search { padding-top: 12px; }
		h1 { font-size: 24px; }
		.panel-search, .panel-filters { padding-left: 16px; padding-right: 16px; }
		.panel-body { padding-left: 16px; padding-right: 16px; }
	}
</style>
