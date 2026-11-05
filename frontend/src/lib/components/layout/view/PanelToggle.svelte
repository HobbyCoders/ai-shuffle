<script lang="ts">
	import { PanelLeft } from 'lucide-svelte';
	import { layout } from '$lib/stores/layout.svelte';
	import { platform } from '$lib/stores/platform.svelte';
</script>
{#if !platform.isMobile}
	<button class="panel-toggle" type="button" aria-label="Hide sidebar" title="Hide sidebar" onclick={() => layout.setSidebarCollapsed(true)}><PanelLeft size={16} strokeWidth={1.8} /></button>
{/if}
<style>
	.panel-toggle { display: none; place-items: center; flex: none; width: 28px; height: 28px; padding: 0; border: 0; border-radius: var(--radius-control); background: transparent; color: var(--sidebar-muted); cursor: pointer; transition: color .2s, background .2s; }
	.panel-toggle:hover { color: var(--sidebar-foreground); background: var(--row-hover); }
	.panel-toggle:active { background: var(--row-selected); }
	.panel-toggle:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	@media (min-width: 768px) { .panel-toggle { display: grid; } }
	@media (prefers-reduced-motion: reduce) { .panel-toggle { transition: none; } }
</style>
