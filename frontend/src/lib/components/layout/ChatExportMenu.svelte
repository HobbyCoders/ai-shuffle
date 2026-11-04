<script lang="ts">
	/**
	 * The chat's one Export control: a single download button that opens a small
	 * menu with the private backup and the sharing export. Replaces two
	 * near-identical icons side by side; both actions stay one click from the menu.
	 */
	import { onDestroy } from 'svelte';
	import { Download } from 'lucide-svelte';
	import { closeDropdown, openHeaderDropdown, toggleDropdown } from '$lib/stores/header-dropdowns';
	import ChatExportActions from './ChatExportActions.svelte';

	/** `up`: opens above the button, for a bar at the bottom of the screen. */
	let { sessionId, up = false }: { sessionId: string; up?: boolean } = $props();

	let root = $state<HTMLElement>();
	const open = $derived($openHeaderDropdown === 'chat-export');

	function onDocPointer(event: PointerEvent) {
		if (open && root && !root.contains(event.target as Node)) closeDropdown('chat-export');
	}
	function onDocKey(event: KeyboardEvent) {
		if (open && event.key === 'Escape') {
			closeDropdown('chat-export');
			root?.querySelector<HTMLElement>('.export-trigger')?.focus();
		}
	}
	onDestroy(() => closeDropdown('chat-export'));
</script>

<svelte:document onpointerdown={onDocPointer} onkeydown={onDocKey} />

<div class="export-menu" bind:this={root}>
	<button
		type="button"
		class="export-trigger"
		class:open
		aria-label="Export chat"
		aria-haspopup="true"
		aria-expanded={open}
		title="Export chat"
		disabled={!sessionId}
		onclick={() => toggleDropdown('chat-export')}
	>
		<Download size={16} strokeWidth={1.5} aria-hidden="true" />
	</button>
	{#if open}
		<div class="export-panel" class:up>
			<ChatExportActions {sessionId} menu onStart={() => closeDropdown('chat-export')} />
		</div>
	{/if}
</div>

<style>
	.export-menu {
		position: relative;
		display: inline-flex;
	}
	.export-trigger {
		display: grid;
		place-items: center;
		width: 28px;
		height: 28px;
		flex-shrink: 0;
		padding: 0;
		border: 0;
		border-radius: 7px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: background-color 0.13s ease, color 0.13s ease;
	}
	.export-trigger:hover:not(:disabled),
	.export-trigger.open {
		background: var(--hover-overlay);
		color: var(--foreground);
	}
	.export-trigger:focus-visible {
		outline: 2px solid var(--ring);
		outline-offset: -2px;
	}
	.export-trigger:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.export-panel {
		position: absolute;
		top: calc(100% + 6px);
		right: 0;
		z-index: 60;
		width: 280px;
		padding: 4px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--popover);
		box-shadow: var(--shadow-popover, 0 12px 32px color-mix(in oklch, var(--foreground) 14%, transparent));
	}
	.export-panel.up {
		top: auto;
		bottom: calc(100% + 6px);
	}
</style>
