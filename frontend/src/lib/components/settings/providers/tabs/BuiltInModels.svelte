<script lang="ts">
	/**
	 * Claude credentials > Models: the built-in roster (not curatable). The
	 * versions a subscription's Claude Code lists (Mythos and others) belong to
	 * the active sign-in, so a cloud, API-key or inactive account's tab shows
	 * only the short ids every sign-in offers.
	 */
	import { getModels } from '$lib/constants/models.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import AvailableModelList from './AvailableModelList.svelte';

	const models = $derived(
		getModels()
			.filter(({ value }) => !value.startsWith('claude-'))
			.map(({ value, label }) => ({ id: value, label }))
	);
</script>

<RowGroup title="Models" count="built in" bare>
	<AvailableModelList {models} emptyMessage="No models reported yet." />
</RowGroup>
