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
	import { ChevronRight, Pin } from 'lucide-svelte';
	import StatusDot from './StatusDot.svelte';
	import Tile from './Tile.svelte';

	interface Props {
		id: string;
		name: string;
		/** Preserve a collection row's existing accessible action name. */
		ariaLabel?: string;
		summary?: string;
		meta?: string;
		icon?: SettingsIcon;
		/** Opt in only when the icon carries information; avatars use lead. Pinned notes keep their mark automatically. */
		showIcon?: boolean;
		/** Item rows stay 48px even without an excerpt; navigation rows are 34px (44px on phone). */
		rowKind?: 'item' | 'navigation';
		disabled?: boolean;
		/** Tile text instead of an icon (provider glyph). */
		glyph?: string;
		status?: StatusTone;
		/** Defaults to the current category. */
		category?: CategoryId;
		selected?: boolean;
		onselect?: (id: string) => void;
		/** Monospace name (log messages, ids). */
		mono?: boolean;
		/** `log`: a bare status dot instead of a tile, tighter row. `pinned`: tinted, outlined row (a pinned item such as the personal assistant). `value`: a 56px settings row, name left, summary right, then a chevron. */
		variant?: 'default' | 'log' | 'pinned' | 'value' | 'plain';
		/** Replaces the tile. */
		lead?: Snippet;
		/** Chips after the name (Draft, Modified). */
		badges?: Snippet;
	}

	let {
		id,
		name,
		ariaLabel,
		summary,
		meta,
		icon,
		showIcon = false,
		disabled = false,
		glyph,
		status,
		category,
		selected,
		onselect,
		mono = false,
		variant = 'default',
		rowKind,
		lead,
		badges
	}: Props = $props();

	const meaningfulIcon = $derived(showIcon || icon === Pin);
	const hasLead = $derived(!!lead || variant === 'log' || (variant !== 'plain' && (!!glyph || meaningfulIcon || !!status)));
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
	class:value={variant === 'value'}
	class:plain={variant === 'plain'}
	class:navigation={(rowKind ?? (variant === 'plain' ? 'navigation' : 'item')) === 'navigation'}
	class:has-lead={hasLead}
	{disabled}
	aria-current={isSelected ? 'true' : undefined}
	aria-label={ariaLabel}
	data-settings-item={id}
	data-settings-nav-item
	onclick={select}
	onkeydown={moveListFocus}
>
	{#if variant === 'value'}
		<span class="nm">{name}</span>
		<span class="vl">{#if status}<StatusDot tone={status} />{/if}<span class="vt">{summary}</span></span>
		<ChevronRight class="chev" aria-hidden="true" />
	{:else}
	{#if lead}
		{@render lead()}
	{:else if variant === 'log'}
		<StatusDot tone={status ?? 'off'} />
	{:else if variant !== 'plain' && (glyph || meaningfulIcon)}
		<Tile {icon} text={glyph} {status} ring={isSelected ? 'accent' : 'bg'} />
	{:else if variant !== 'plain' && status}
		<StatusDot tone={status} />
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
	{/if}
</button>

<style>
	.st-it {
		position: relative;
		display: grid;
		grid-template-columns: minmax(0, 1fr) auto;
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
	.st-it.has-lead { grid-template-columns: auto minmax(0, 1fr) auto; }
	.st-it.navigation { min-height: 34px; padding-block: 4px; }
	.st-it.navigation .nm { font-size: 13px; }
	.st-it:active:not(:disabled) { background: var(--st-accent); }
	.st-it:disabled { opacity: .45; cursor: not-allowed; }
	@media (max-width: 600px), (pointer: coarse) { .st-it.navigation { min-height: 44px; } }
	.st-it.log:not(.navigation) { min-height: 48px; }
	.st-it.value {
		grid-template-columns: minmax(auto, 40%) minmax(0, 1fr) 16px;
		gap: 12px;
		min-height: 56px;
		padding: 6px 16px;
		border-radius: 0;
	}
	.st-it.value:first-child {
		border-radius: var(--st-radius-lg) var(--st-radius-lg) 0 0;
	}
	.st-it.value:last-child {
		border-radius: 0 0 var(--st-radius-lg) var(--st-radius-lg);
	}
	.st-it.value:only-child {
		border-radius: var(--st-radius-lg);
	}
	.st-it.value + :global(.st-it.value) {
		border-top: 1px solid var(--st-border);
	}
	.vl {
		display: flex;
		align-items: center;
		justify-content: flex-end;
		gap: 7px;
		min-width: 0;
		font-size: 13px;
		color: var(--st-mfg);
	}
	.vt {
		min-width: 0;
		white-space: nowrap;
		overflow: hidden;
		text-overflow: ellipsis;
	}
	.st-it.value :global(.chev) {
		width: 16px;
		height: 16px;
		color: var(--st-subtle);
	}
	.st-it.pinned {
		border: 1px solid var(--st-primary-line);
		background: var(--st-primary-soft);
	}
	.st-it.pinned[aria-current='true'] {
		background: color-mix(in oklab, var(--st-primary) 20%, var(--st-accent));
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
	@media (max-width: 600px) { .st-it:not(.value) { padding-inline: 0; } .nm, .st-it.navigation .nm { font-size: 15px; } }
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
		font-size: 12px;
		color: var(--st-subtle);
		white-space: nowrap;
		font-variant-numeric: tabular-nums;
	}
</style>
