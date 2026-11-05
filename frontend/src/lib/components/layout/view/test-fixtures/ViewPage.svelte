<script lang="ts">
	import { setContext } from 'svelte';
	import { setSettingsAccess } from '$lib/settings/access.svelte';
	import { setRowContext } from '../../../settings/rows/row-context';
	import { FEATURE_PANEL_CONTEXT } from '../../feature-panel-context';
	import ViewShell from '../ViewShell.svelte';
	import ViewPanel from '../ViewPanel.svelte';
	import PanelProbe from './PanelProbe.svelte';
	let { active = true, contentFirst = false, readOnly = true }: { active?: boolean; contentFirst?: boolean; readOnly?: boolean } = $props();
	setContext(FEATURE_PANEL_CONTEXT, { get active() { return active; } });
	setSettingsAccess({ get readOnly() { return readOnly; } });
	setRowContext({ id: 'probe', label: 'Bridged switch', labelId: 'probe-label', hintId: undefined, expansionId: 'probe-expand' });
	let detail = $state(false);
</script>
<span id="probe-label">Bridged switch</span>
<ViewShell label="Probe" {contentFirst} detailOpen={detail} onBack={() => detail = false}>
	{#snippet panel()}
		<ViewPanel title="Probe"><PanelProbe /><button onclick={() => detail = true}>Open detail</button></ViewPanel>
	{/snippet}
	{#snippet main(back)}<h2>Probe detail</h2><button onclick={back}>Back to panel</button>{/snippet}
</ViewShell>
