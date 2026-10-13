<script lang="ts">
	/** Hosts DesktopUpdateButton the way the sidebar does: binds `canCheck`
	 *  and drives the exported manual check from an outside control (#1623). */
	import DesktopUpdateButton from '$lib/components/layout/sidebar/DesktopUpdateButton.svelte';

	let updater = $state<{ checkForUpdates: () => Promise<void> } | null>(null);
	let canCheck = $state(false);
</script>

<DesktopUpdateButton bind:this={updater} bind:canCheck />
<output data-testid="can-check">{canCheck ? 'yes' : 'no'}</output>
<button type="button" onclick={() => void updater?.checkForUpdates()}>Manual check</button>
