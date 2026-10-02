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
	import { ChevronLeft } from 'lucide-svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { categoryById } from '$lib/settings/categories';
	import type { CategoryId } from '$lib/settings/types';
	import SavedTick from './SavedTick.svelte';

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
		<div class="st-page-h">
			<button type="button" class="st-back" aria-label="Back to Settings" onclick={() => settingsNav.back()}>
				<ChevronLeft />
			</button>
			<h1 tabindex="-1" data-settings-heading="page">{label}</h1>
			<SavedTick />
		</div>
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
		max-width: 760px;
		padding: 32px 48px 120px;
	}
	.st-page-h {
		display: flex;
		align-items: center;
		gap: 12px;
		margin-bottom: 26px;
		min-height: 32px;
	}
	h1 {
		font-size: 22px;
		font-weight: 600;
		letter-spacing: -0.012em;
		margin: 0;
	}
	h1:focus {
		outline: none;
	}
</style>
