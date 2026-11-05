<script lang="ts">
	/** Same geometry, tokens and type as the Chats search field. */
	import { Search, X } from 'lucide-svelte';
	import PanelIconButton from './PanelIconButton.svelte';
	let { value = $bindable(''), placeholder = 'Search', label = placeholder, disabled = false,
		input = $bindable(), oninput, onkeydown, clearable = false }: {
		value?: string; placeholder?: string; label?: string; disabled?: boolean; clearable?: boolean;
		input?: HTMLInputElement;
		oninput?: (event: Event & { currentTarget: HTMLInputElement }) => void;
		onkeydown?: (event: KeyboardEvent) => void;
	} = $props();
</script>
<div class="panel-search-field" class:clearable>
	<Search size={15} strokeWidth={1.75} aria-hidden="true" />
	<input bind:this={input} type="search" bind:value {placeholder} aria-label={label} {disabled} {oninput} {onkeydown} />
	{#if clearable && value}<PanelIconButton label="Clear search" icon={X} {disabled} onclick={() => { value = ''; input?.focus(); }} />{/if}
</div>
<style>
	.panel-search-field { display: flex; align-items: center; gap: 9px; height: 32px; padding: 0 10px 0 11px; border: 1px solid var(--field-border); border-radius: var(--radius-control); background: var(--field-bg); color: var(--sidebar-muted); transition: border-color 0.2s ease-out; }
	.panel-search-field:hover { color: var(--sidebar-foreground); }
	.panel-search-field:focus-within { border-color: var(--field-border-focus); outline: 2px solid var(--primary); outline-offset: 2px; }
	.panel-search-field:has(input:disabled) { opacity: 0.5; }
	input { width: 100%; min-width: 0; height: 100%; border: 0; padding: 0; outline: none; background: transparent; color: var(--foreground); font: inherit; font-size: 13px; }
	input::placeholder { color: var(--sidebar-muted); opacity: 1; }
	.clearable input::-webkit-search-cancel-button { display: none; }
	@media (max-width: 600px), (pointer: coarse) { .panel-search-field { height: 44px; } input { font-size: 16px; } }
	@media (prefers-reduced-motion: reduce) { .panel-search-field { transition: none; } }
</style>
