<script lang="ts">
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
	<select bind:value {disabled}>
		<option value="">Use Settings default</option>
		{#each catalog.groups as group (group.key)}
			<optgroup label={group.subtext ? `${group.name} · ${group.subtext}` : group.name}>
				{#each catalog.options.filter((option) => option.groupKey === group.key) as option (option.value)}
					<option value={option.value}>{option.label}</option>
				{/each}
			</optgroup>
		{/each}
	</select>
	<small>Used only to generate this content. Does not change Settings or the models assigned to agents.</small>
</label>

<style>
	.generation-model { display: flex; flex-direction: column; gap: 0.5rem; margin-bottom: 1rem; min-width: 0; }
	span { font-size: 0.8125rem; font-weight: 600; color: var(--foreground); }
	select { width: 100%; min-width: 0; padding: 0.625rem 0.75rem; border: 1px solid var(--border); border-radius: 0.5rem; background: var(--background); color: var(--foreground); font: inherit; font-size: 0.8125rem; }
	select:focus-visible { outline: 2px solid var(--primary); outline-offset: 2px; }
	select:disabled { opacity: 0.5; }
	small { font-size: 0.75rem; line-height: 1.5; color: var(--muted-foreground); }
</style>
