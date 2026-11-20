<script lang="ts">
	/**
	 * Claude credentials > Models for a cloud, standby or inactive Claude
	 * credential: the four short ids under plain family names. The loaded model
	 * list may still describe another sign-in (a subscription's listed versions
	 * and labels such as "Haiku 5.5", or the one active before a switch whose
	 * reload has not landed), so this tab never reads it.
	 */
	import { getBuiltInAliases } from '$lib/constants/models.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import AvailableModelList from './AvailableModelList.svelte';

	const models = getBuiltInAliases().map(({ value, label }) => ({ id: value, label }));
</script>

<RowGroup title="Models" count="built in" bare>
	<AvailableModelList {models} emptyMessage="No models reported yet." />
</RowGroup>
