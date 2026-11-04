<script lang="ts">
	/** A provider's tile: its brand mark, or one line icon for media and gateways. */
	import { Clapperboard, Server } from 'lucide-svelte';
	import type { StatusTone } from '$lib/settings/types';
	import ProviderGlyph from '../ProviderGlyph.svelte';
	import Tile from '../rows/Tile.svelte';
	import type { ProviderView } from './providers-model';

	interface Props {
		provider: Pick<ProviderView, 'glyph'> & { media?: boolean };
		size?: 'sm' | 'lg';
		status?: StatusTone;
		ring?: 'bg' | 'accent';
		dashed?: boolean;
		/** The list's mark: the glyph alone at 16px, no bordered tile. */
		bare?: boolean;
	}

	let { provider, size = 'sm', status, ring = 'bg', dashed = false, bare = false }: Props = $props();
	const mark = $derived(provider.glyph !== 'custom');
</script>

{#if bare}
	<span class="bare" aria-hidden="true">
		{#if mark}
			<ProviderGlyph kind={provider.glyph} size={16} />
		{:else if provider.media}
			<Clapperboard size={16} />
		{:else}
			<Server size={16} />
		{/if}
	</span>
{:else}
<Tile {size} {status} {ring} {dashed}>
	{#if mark}
		<ProviderGlyph kind={provider.glyph} size={size === 'lg' ? 19 : 14} />
	{:else if provider.media}
		<Clapperboard />
	{:else}
		<Server />
	{/if}
</Tile>
{/if}

<style>
	.bare {
		display: grid;
		place-items: center;
		flex: none;
		width: 16px;
		height: 16px;
		color: var(--st-fg);
	}
</style>
