<script lang="ts">
	/**
	 * Claude credentials > Models: the built-in roster (not curatable). The
	 * versions a subscription's Claude Code lists (Mythos and others) belong to
	 * the active sign-in, so this tab shows only the short ids. The running
	 * credential labels them with the versions they run; a standby or inactive
	 * one uses plain family names, since another sign-in's versions say nothing
	 * about it.
	 */
	import { getBuiltInAliases, getModels } from '$lib/constants/models.svelte';
	import RowGroup from '../../rows/RowGroup.svelte';
	import AvailableModelList from './AvailableModelList.svelte';

	interface Props {
		/** This credential is the one Claude sessions run on now. */
		active?: boolean;
	}

	let { active = false }: Props = $props();

	const models = $derived(
		(active ? getModels() : getBuiltInAliases())
			.filter(({ value }) => !value.startsWith('claude-'))
			.map(({ value, label }) => ({ id: value, label }))
	);
</script>

<RowGroup title="Models" count="built in" bare>
	<AvailableModelList {models} emptyMessage="No models reported yet." />
</RowGroup>
