<script lang="ts">
	/**
	 * PreferencePage: one column of RowGroups under the category title, with
	 * the Saved tick in the header.
	 *
	 *   <PreferencePage category="general">
	 *     <RowGroup title="Appearance">
	 *       <SettingRow id="general.theme"><Segmented ... /></SettingRow>
	 *     </RowGroup>
	 *   </PreferencePage>
	 */
	import type { Snippet } from 'svelte';
	import BackRow from './BackRow.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { categoryById } from '$lib/settings/categories';
	import type { CategoryId } from '$lib/settings/types';
	import SavedTick from './SavedTick.svelte';
	import PageTitle from './PageTitle.svelte';

	interface Props {
		category: CategoryId;
		/** Defaults to the category label. */
		title?: string;
		children: Snippet;
	}

	let { category, title, children }: Props = $props();
	const label = $derived(title ?? categoryById(category).label);
</script>

<div class="st-pscroll st-scroll">
	<div class="st-page">
		<BackRow label="Settings" onclick={() => settingsNav.back()} />
		<PageTitle title={label}>{#snippet actions()}<SavedTick />{/snippet}</PageTitle>
		{@render children()}
	</div>
</div>

<style>
	.st-pscroll {
		flex: 1;
		overflow-y: auto;
		min-height: 0;
	}
	.st-page {
		position: relative;
		max-width: calc(var(--st-reading-width) + 2 * var(--st-gutter));
		margin: 0 auto;
		padding: var(--st-page-top) var(--st-gutter) 120px;
	}

</style>
