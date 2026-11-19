<script lang="ts">
	/** Claude credentials > Models: the built-in roster (not curatable). */
	import { getModels } from '$lib/constants/models.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import AvailableModelList from './AvailableModelList.svelte';

	const models = $derived(getModels().map(({ value, label }) => ({ id: value, label })));
</script>

<RowGroup title="Models" count="built in" bare>
	<AvailableModelList {models} emptyMessage="No models reported yet." />
</RowGroup>
