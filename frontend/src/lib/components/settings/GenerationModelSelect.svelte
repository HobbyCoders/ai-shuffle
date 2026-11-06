<script lang="ts">
	import SelectField from './rows/SelectField.svelte';
	import { buildModelPickerCatalog } from '$lib/constants/model-picker';

	interface Props {
		value?: string;
		disabled?: boolean;
	}

	let { value = $bindable(''), disabled = false }: Props = $props();
	const catalog = $derived(buildModelPickerCatalog());
</script>

<label class="generation-model">
	<span>Generation model</span>
	<SelectField bind:value {disabled} label="Generation model" width={640}
		groups={[
			{ label: '', options: [{ value: '', label: 'Use Settings default' }] },
			...catalog.groups.map((group) => ({
				label: group.subtext ? `${group.name} · ${group.subtext}` : group.name,
				options: catalog.options.filter((option) => option.groupKey === group.key)
			}))
		]} />
	<small>Used only to generate this content. Does not change Settings or the models assigned to agents.</small>
</label>

<style>
	.generation-model { display: flex; flex-direction: column; gap: 0.5rem; margin-bottom: 1rem; min-width: 0; }
	span { font-size: 0.8125rem; font-weight: 600; color: var(--foreground); }
	small { font-size: 0.75rem; line-height: 1.5; color: var(--muted-foreground); }
</style>
