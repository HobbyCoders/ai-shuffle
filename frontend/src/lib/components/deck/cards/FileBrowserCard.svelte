<script lang="ts">
	/**
	 * FileBrowserCard - File browser card
	 *
	 * Wraps the existing FilesView component in a FeaturePageLayout.
	 * Provides full file management: browse, upload, download, rename, delete.
	 */

	import FeaturePageLayout from '$lib/components/layout/FeaturePageLayout.svelte';
	import { FilesView } from '../files';
	import { FolderTree } from 'lucide-svelte';

	interface Props {
		mobile?: boolean;
	}

	let { mobile = false }: Props = $props();
</script>

<FeaturePageLayout icon={FolderTree} title="Files" noPadding={true}>
	{#snippet children()}
		<div class="files-card-content">
			<FilesView />
		</div>
	{/snippet}
</FeaturePageLayout>

<style>
	.files-card-content {
		flex: 1;
		display: flex;
		flex-direction: column;
		overflow: hidden;
		min-height: 0;
		border-radius: 0 0 12px 12px;
	}

	.files-card-content.mobile {
		height: 100%;
		border-radius: 0;
	}

	/* Ensure FilesView fills the card */
	.files-card-content :global(.files-view) {
		flex: 1;
		min-height: 0;
	}
</style>
