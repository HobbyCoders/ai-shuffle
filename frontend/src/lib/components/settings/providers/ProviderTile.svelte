<script lang="ts">
	/** A provider's tile: its brand mark, or a letter for fal.ai and gateways. */
	import type { StatusTone } from '$lib/settings/types';
	import ProviderGlyph from '../ProviderGlyph.svelte';
	import Tile from '../rows/Tile.svelte';
	import type { ProviderView } from './providers-model';

	interface Props {
		provider: Pick<ProviderView, 'glyph' | 'letter'>;
		size?: 'sm' | 'lg';
		status?: StatusTone;
		ring?: 'bg' | 'accent';
		dashed?: boolean;
	}

	let { provider, size = 'sm', status, ring = 'bg', dashed = false }: Props = $props();
	const mark = $derived(provider.glyph !== 'custom');
</script>

<Tile {size} {status} {ring} {dashed}>
	{#if mark}
		<ProviderGlyph kind={provider.glyph} size={size === 'lg' ? 19 : 14} />
	{:else}
		{provider.letter}
	{/if}
</Tile>
