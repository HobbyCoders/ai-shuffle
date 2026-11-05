<script lang="ts">
	import ViewPanel from '../ViewPanel.svelte';
	import EmptyDetail from '../../../settings/shell/EmptyDetail.svelte';
	let { filtered = false }: { filtered?: boolean } = $props();
</script>
<ViewPanel title="Examples">
	{#snippet actions()}<button>New item</button>{/snippet}
	<EmptyDetail title={filtered ? 'No matching items' : 'No items yet'} message="Create an item to get started."
		actionLabel={filtered ? 'Clear search' : undefined} onAction={() => filtered = false}>
		<button>New item</button>
	</EmptyDetail>
</ViewPanel>
