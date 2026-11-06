<script lang="ts">
	/**
	 * A provider's tile: its brand mark, or one line icon for media and gateways.
	 * Every list row (group headers, not-connected rows, the tuning list) uses the
	 * same 28px tile with a 10% text-colour fill and a 16px mark, on desktop and
	 * phone, so every row starts identically. `lg` is the detail header's tile.
	 */
	import { Clapperboard, Server } from 'lucide-svelte';
	import type { StatusTone } from '$lib/settings/types';
	import ProviderGlyph from '../ProviderGlyph.svelte';
	import StatusDot from '../rows/StatusDot.svelte';
	import Tile from '../rows/Tile.svelte';
	import type { ProviderView } from './providers-model';

	interface Props {
		provider: Pick<ProviderView, 'glyph'> & { media?: boolean };
		size?: 'sm' | 'lg';
		status?: StatusTone;
		ring?: 'bg' | 'accent';
		dashed?: boolean;
		/** Kept for callers: list rows always get the 28px provider tile. */
		bare?: boolean;
	}

	let { provider, size = 'sm', status, ring = 'bg', dashed = false }: Props = $props();
	const mark = $derived(provider.glyph !== 'custom');
</script>

{#if size === 'sm'}
	<span class="pt" class:dashed aria-hidden="true">
		{#if mark}
			<ProviderGlyph kind={provider.glyph} size={16} tiled />
		{:else if provider.media}
			<Clapperboard size={16} />
		{:else}
			<Server size={16} />
		{/if}
		{#if status}
			<span class="corner"><StatusDot tone={status} {ring} size={9} /></span>
		{/if}
	</span>
{:else}
	<Tile {size} {status} {ring} {dashed}>
		{#if mark}
			<ProviderGlyph kind={provider.glyph} size={19} tiled />
		{:else if provider.media}
			<Clapperboard />
		{:else}
			<Server />
		{/if}
	</Tile>
{/if}

<style>
	.pt {
		position: relative;
		display: grid;
		place-items: center;
		flex: none;
		width: 28px;
		height: 28px;
		border-radius: 7px;
		background: color-mix(in oklch, var(--st-fg) 10%, transparent);
		color: var(--st-fg);
	}
	.pt.dashed {
		color: var(--st-mfg);
	}
	.corner {
		position: absolute;
		right: -3px;
		bottom: -3px;
		display: flex;
	}
</style>
