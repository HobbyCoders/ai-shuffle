<script lang="ts">
	import { setContext } from 'svelte';
	import { FEATURE_PANEL_CONTEXT } from '$lib/components/layout/feature-panel-context';
	import PanelHost from '$lib/components/layout/view/test-fixtures/PanelHost.svelte';
	import NotesCard from '../NotesCard.svelte';
	let { active = true }: { active?: boolean } = $props();
	setContext(FEATURE_PANEL_CONTEXT, { get active() { return active; } });
</script>
<PanelHost />
<div hidden={!active}><NotesCard /></div>
