<script lang="ts">
	// EmptyDetail with an icon and a create action, as Settings > Plugins uses it.
	import { Puzzle } from 'lucide-svelte';
	import EmptyDetail from '../EmptyDetail.svelte';
	let { full = false }: { full?: boolean } = $props();
</script>
<EmptyDetail icon={Puzzle} title="No plugins yet" message="Plugins add skills to your agents." {full}>
	<button type="button">Add plugin</button>
</EmptyDetail>
