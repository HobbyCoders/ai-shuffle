<script lang="ts">
	/** A provider with nothing connected: the kinds it supports, each with Set up. */
	import DetailPane from '../shell/DetailPane.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ProviderTile from './ProviderTile.svelte';
	import { kindsOf, type ProviderView } from './providers-model';
	import { startSetup } from './provider-actions';

	interface Props {
		provider: ProviderView;
	}

	let { provider }: Props = $props();
	const kinds = $derived(kindsOf(provider.id));
	const models = $derived(
		provider.claude
			? 'Built in'
			: provider.media
				? 'Media models'
				: provider.id === 'openrouter'
					? 'Hundreds'
					: 'From the key'
	);
</script>

<DetailPane
	name={provider.name}
	badge="Provider"
	status="off"
	statusText="Not connected"
	subtitle="Nothing added yet"
	facts={[
		{ label: 'Kinds', value: kinds.map((k) => k.label).join(', ') },
		{ label: 'Models', value: models }
	]}
	item={provider.item}
>
	{#snippet tile()}<ProviderTile {provider} size="lg" dashed />{/snippet}
	<RowGroup title="Connect" id="providers-connect">
		{#each kinds as k (k.flavor)}
			<SettingRow label={k.label} hint={k.hint}>
				<button type="button" class="st-btn primary" onclick={() => startSetup(provider, k.flavor)}>Set up</button>
			</SettingRow>
		{/each}
	</RowGroup>
</DetailPane>
