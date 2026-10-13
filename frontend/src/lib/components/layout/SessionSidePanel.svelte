<script lang="ts">
	import { onDestroy, type Snippet } from 'svelte';
	import { Maximize2, Minimize2, X } from 'lucide-svelte';
	import type { SidePanelView } from '$lib/stores/side-panel.svelte';
	let { active, expanded, width, mobile = false,
		onClose, onResize, onExpand, children }: {
		active: SidePanelView; expanded: boolean; width: number; mobile?: boolean;
		onClose: () => void;
		onResize: (width: number) => void; onExpand: () => void; children: Snippet<[Snippet]>;
	} = $props();
	let element: HTMLElement;
	let resizing = $state(false);
	let cleanupResize = () => {};
	onDestroy(() => cleanupResize());
	function startResize(event: PointerEvent) {
		if (event.button !== 0) return;
		event.preventDefault();
		cleanupResize();
		const start = event.clientX;
		const initial = element.getBoundingClientRect().width;
		const maximum = element.parentElement?.clientWidth ?? initial;
		resizing = true;
		const move = (e: PointerEvent) => onResize(Math.min(maximum, initial + start - e.clientX));
		cleanupResize = () => {
			resizing = false;
			window.removeEventListener('pointermove', move);
			window.removeEventListener('pointerup', cleanupResize);
			window.removeEventListener('pointercancel', cleanupResize);
		};
		window.addEventListener('pointermove', move);
		window.addEventListener('pointerup', cleanupResize);
		window.addEventListener('pointercancel', cleanupResize);
	}
</script>

<svelte:window onkeydown={(e) => {
	// Changes owns Escape so its confirmation dialog gets the first dismissal.
	if (active === 'browser' && e.key === 'Escape' && !e.defaultPrevented) onClose();
	// Inside a terminal, Esc belongs to the shell (vim, less, readline).
	else if (active === 'terminal' && e.key === 'Escape' && !e.defaultPrevented
		&& !(e.target instanceof Element && e.target.closest('.xterm, .tpanel input, .tpanel [role="alertdialog"]'))) onClose();
}} />

<aside bind:this={element} class="side-panel" class:expanded class:mobile class:resizing
	style:--panel-width={`${width}px`} aria-label="Session side panel">
	{#if !expanded && !mobile}
		<div class="resize-handle" role="separator" aria-label="Resize side panel"
			aria-orientation="vertical" onpointerdown={startResize}></div>
	{/if}
	{#snippet panelActions()}
		<div class="panel-actions">
			{#if !mobile}<button onclick={onExpand} aria-label={expanded ? 'Restore side panel' : 'Expand side panel'} title={expanded ? 'Restore side panel' : 'Expand side panel'}>
				{#if expanded}<Minimize2 size={16} />{:else}<Maximize2 size={16} />{/if}
			</button>{/if}
			<button onclick={onClose} aria-label="Close side panel" title="Close side panel (Esc)"><X size={18} /></button>
		</div>
	{/snippet}
	<div class="panel-content">{@render children(panelActions)}</div>
</aside>

<style>
	.side-panel { position: relative; flex: 0 0 auto; width: var(--panel-width); max-width: 70%; min-width: 0; min-height: 0; display: flex; flex-direction: column; background: var(--background); border-left: 1px solid var(--border); z-index: 20; }
	.side-panel.expanded, .side-panel.mobile { position: absolute; inset: 0; width: 100%; max-width: 100%; border: 0; }
	.side-panel.mobile { padding-bottom: env(safe-area-inset-bottom, 0px); }
	.panel-actions { display: flex; align-items: center; gap: 4px; flex-shrink: 0; -webkit-app-region: no-drag; }
	button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-width: 32px; min-height: 32px; padding: 6px; border: 0; border-radius: 6px; background: transparent; color: var(--muted-foreground); font-size: 12px; cursor: pointer; }
	button:hover { background: var(--muted); color: var(--foreground); }
	button:focus-visible { outline: 2px solid var(--ring); outline-offset: -2px; }
	.panel-content { flex: 1; min-height: 0; min-width: 0; overflow: hidden; }
	.resize-handle { position: absolute; inset: 0 auto 0 -4px; width: 8px; cursor: col-resize; z-index: 30; touch-action: none; }
	.resize-handle:hover { background: color-mix(in oklch, var(--ring) 30%, transparent); }
	.resizing { user-select: none; }
	.resizing .panel-content { pointer-events: none; }
	@media (max-width: 700px) { .side-panel { position: absolute; inset: 0 0 0 auto; max-width: 100%; } }
</style>
