<script lang="ts">
	/**
	 * Tile: the square that stands for an object, with an optional status dot
	 * on its corner. `sm` (26px) in lists, `lg` (42px) in a detail header.
	 * Pass an `icon` component or short `text` (a provider glyph), or `children`.
	 */
	import type { Snippet } from 'svelte';
	import type { SettingsIcon } from '$lib/settings/categories';
	import type { StatusTone } from '$lib/settings/types';
	import StatusDot from './StatusDot.svelte';

	interface Props {
		icon?: SettingsIcon;
		text?: string;
		status?: StatusTone;
		size?: 'sm' | 'lg';
		/** Dot ring: the list row's background (accent when selected). */
		ring?: 'bg' | 'accent';
		dashed?: boolean;
		children?: Snippet;
	}

	let { icon: Icon, text, status, size = 'sm', ring = 'bg', dashed = false, children }: Props = $props();
</script>

<span class="st-tile {size}" class:dashed aria-hidden="true">
	{#if children}{@render children()}{:else if Icon}<Icon />{:else if text}{text}{/if}
	{#if status}
		<span class="corner"><StatusDot tone={status} {ring} size={size === 'lg' ? 11 : 9} /></span>
	{/if}
</span>

<style>
	.st-tile {
		position: relative;
		display: grid;
		place-items: center;
		flex: none;
		background: var(--st-muted);
		border: 1px solid var(--st-border);
		color: var(--st-fg);
		font-weight: 600;
	}
	.st-tile.sm {
		width: 26px;
		height: 26px;
		border-radius: 7px;
		font-size: 12px;
	}
	.st-tile.lg {
		width: 42px;
		height: 42px;
		border-radius: 10px;
		font-size: 16px;
	}
	.st-tile.sm :global(svg.lucide) {
		width: 14px;
		height: 14px;
	}
	.st-tile.lg :global(svg.lucide) {
		width: 19px;
		height: 19px;
	}
	.st-tile.dashed {
		border-style: dashed;
		color: var(--st-mfg);
	}
	.corner {
		position: absolute;
		right: -3px;
		bottom: -3px;
		display: flex;
	}
</style>
