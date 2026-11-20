<script lang="ts">
	import ModelPicker from './ModelPicker.svelte';

	interface Props {
		value?: string;
		disabled?: boolean;
	}

	let { value = $bindable(''), disabled = false }: Props = $props();
</script>

<div class="generation-model">
	<span>Generation model</span>
	<ModelPicker {value} {disabled} label="Generation model" formSelect allowEmpty allowCustom={false}
		emptyLabel="Use Settings default" onSelect={(selected) => (value = selected)} />
	<small>Used only to generate this content. Does not change Settings or the models assigned to agents.</small>
</div>

<style>
	.generation-model { display: flex; flex-direction: column; gap: 0.5rem; margin-bottom: 1rem; min-width: 0; }
	span { font-size: 0.8125rem; font-weight: 600; color: var(--foreground); }
	small { font-size: 0.75rem; line-height: 1.5; color: var(--muted-foreground); }
</style>
