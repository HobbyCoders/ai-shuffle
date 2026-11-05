<script lang="ts">
	/** Chats' column anatomy, including its own Settings token scope when docked. */
	import type { Snippet } from 'svelte';
	import { platform } from '$lib/stores/platform.svelte';
	import '../../settings/shell/settings.css';
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
</script>

<!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
<section class="settings-shell view-panel" class:native={platform.isMobile} aria-label={title} aria-busy={loading || undefined} {onkeydown}>
	<header class="panel-header">
		<h1>{title}</h1>
		{#if count !== undefined}<span class="panel-count">{count}</span>{/if}
		<div class="panel-actions">{@render actions?.()}</div>
	</header>
	{#if search}<div class="panel-search">{@render search()}</div>{/if}
	{#if filters}<div class="panel-filters">{@render filters()}</div>{/if}
	<div class="panel-body st-scroll" data-settings-list role="region" aria-label={listLabel ?? `${title} list`}>
		{@render children?.()}
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
	.panel-footer { flex: none; padding: 8px; }
	/* The real phone shell already names the page in its app bar. Keep actions. */
	.native .panel-header { height: 48px; }
	.native .panel-header:has(.panel-actions:empty) { display: none; }
	.native h1, .native .panel-count { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
	.native .panel-search { padding-top: 12px; }
	@media (max-width: 600px) {
		.view-panel { background: var(--background); }
		.panel-header { height: 60px; padding-left: 20px; }
		h1 { font-size: 24px; }
		.panel-search, .panel-filters { padding-left: 16px; padding-right: 16px; }
		.panel-body { padding-left: 16px; padding-right: 16px; }
	}
</style>
