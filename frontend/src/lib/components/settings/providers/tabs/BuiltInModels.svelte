<script lang="ts">
	/** Claude credentials > Models: the built-in roster (not curatable). */
	import { getModels } from '$lib/constants/models.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import StatusDot from '../../rows/StatusDot.svelte';

	const models = $derived(getModels());
</script>

<RowGroup title="Models" count="built in" bare>
	<div class="st-card pv-card">
		<div class="pv-mlist">
			{#each models as m (m.value)}
				<div class="pv-mrow static">
					<StatusDot tone="ok" />
					<span class="id">{m.value}</span>
					<span class="lbl">{m.label}</span>
				</div>
			{:else}
				<p class="pv-empty">No models reported yet.</p>
			{/each}
		</div>
	</div>
</RowGroup>
