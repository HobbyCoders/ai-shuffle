<script lang="ts" module>
	/**
	 * Up/Down between list rows. Any focusable element in a collection list can
	 * join by carrying `data-settings-nav-item` (ListItem does).
	 */
	export function moveListFocus(event: KeyboardEvent): void {
		if (event.altKey || (event.key !== 'ArrowDown' && event.key !== 'ArrowUp')) return;
		const from = event.currentTarget as HTMLElement;
		const scope = from.closest('[data-settings-list]') ?? from.parentElement;
		if (!scope) return;
		const all = Array.from(scope.querySelectorAll<HTMLElement>('[data-settings-nav-item]')).filter(
			(el) => el === from || (!el.hasAttribute('disabled') && !el.closest('[hidden]'))
		);
		const next = all[all.indexOf(from) + (event.key === 'ArrowDown' ? 1 : -1)];
		if (next) {
			event.preventDefault();
			next.focus();
		}
	}
</script>

<script lang="ts">
	/**
	 * ListItem: one object in a collection list. Tile with status dot, name,
	 * one-line summary, optional meta on the right. Selection comes from the
	 * navigation store unless `selected`/`onselect` are given.
	 */
	import type { Snippet } from 'svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import type { SettingsIcon } from '$lib/settings/categories';
	import type { CategoryId, StatusTone } from '$lib/settings/types';
	import StatusDot from './StatusDot.svelte';
	import Tile from './Tile.svelte';

	interface Props {
		id: string;
		name: string;
		summary?: string;
		meta?: string;
		icon?: SettingsIcon;
		/** Tile text instead of an icon (provider glyph). */
		glyph?: string;
		status?: StatusTone;
		/** Defaults to the current category. */
		category?: CategoryId;
		selected?: boolean;
		onselect?: (id: string) => void;
		/** Monospace name (log messages, ids). */
		mono?: boolean;
		/** `log`: a bare status dot instead of a tile, tighter row. `pinned`: tinted, outlined row (a pinned item such as the personal assistant). */
		variant?: 'default' | 'log' | 'pinned';
		/** Replaces the tile. */
		lead?: Snippet;
		/** Chips after the name (Draft, Modified). */
		badges?: Snippet;
	}

	let {
		id,
		name,
		summary,
		meta,
		icon,
		glyph,
		status,
		category,
		selected,
		onselect,
		mono = false,
		variant = 'default',
		lead,
		badges
	}: Props = $props();

	const cat = $derived(category ?? settingsNav.category);
	const isSelected = $derived(selected ?? settingsNav.selectedItem(cat) === id);

	function select() {
		if (onselect) onselect(id);
		else settingsNav.selectItem(id, cat);
	}
</script>

<button
	type="button"
	class="st-it"
	class:log={variant === 'log'}
	class:pinned={variant === 'pinned'}
	aria-current={isSelected ? 'true' : undefined}
	data-settings-item={id}
	data-settings-nav-item
	onclick={select}
	onkeydown={moveListFocus}
>
	{#if lead}
		{@render lead()}
	{:else if variant === 'log'}
		<StatusDot tone={status ?? 'off'} />
	{:else}
		<Tile {icon} text={glyph} {status} ring={isSelected ? 'accent' : 'bg'} />
	{/if}
	<span class="tx">
		{#if badges}
			<span class="nml"><span class="nm" class:st-mono={mono}>{name}</span>{@render badges()}</span>
		{:else}
			<span class="nm" class:st-mono={mono}>{name}</span>
		{/if}
		{#if summary}<span class="sm">{summary}</span>{/if}
	</span>
	{#if meta}<span class="meta">{meta}</span>{:else}<span></span>{/if}
</button>

<style>
	.st-it {
		position: relative;
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 10px;
		min-height: 48px;
		width: 100%;
		padding: 6px 10px 6px 12px;
		border: 0;
		border-radius: var(--st-radius-md);
		background: none;
		text-align: left;
		color: inherit;
	}
	.st-it:hover {
		background: var(--st-hover);
	}
	.st-it[aria-current='true'] {
		background: var(--st-accent);
	}
	.st-it:focus-visible {
		outline-offset: -2px;
	}
	.st-it.log {
		min-height: 42px;
	}
	.st-it.pinned {
		border: 1px solid var(--st-primary-line);
		background: var(--st-primary-soft);
	}
	.st-it.pinned[aria-current='true'] {
		background: color-mix(in oklch, var(--st-primary) 20%, var(--st-accent));
	}
	.st-it.pinned .meta {
		color: var(--st-mfg);
	}
	.nml {
		display: flex;
		align-items: center;
		gap: 6px;
		min-width: 0;
	}
	.tx {
		min-width: 0;
		display: grid;
	}
	.nm {
		font-size: 13.5px;
		font-weight: 500;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.nm.st-mono {
		font-weight: 400;
		font-size: 12px;
	}
	.sm {
		font-size: 12px;
		color: var(--st-mfg);
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.meta {
		font-size: 11.5px;
		color: var(--st-subtle);
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
		font-family: var(--st-mono);
	}
</style>
