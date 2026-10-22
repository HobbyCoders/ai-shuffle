<script lang="ts">
	/**
	 * Settings > Providers > Claude > Sessions: whether Claude sessions compact
	 * through AI Shuffle (on by default). Owner-wide, read when a Claude
	 * session starts. A failed save flips the switch back and says why.
	 */
	import { onMount } from 'svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { getClaudeCompaction, updateClaudeCompaction } from '$lib/api/claude-compaction';
	import { FORBIDDEN_TEXT, isForbidden } from '$lib/settings/access.svelte';
	import { getEntry } from '$lib/settings/registry';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import Switch from '../rows/Switch.svelte';
	import ValueText from '../rows/ValueText.svelte';

	const ID = 'providers.claudeCompaction';

	let enabled = $state<boolean | null>(null);
	let loadError = $state('');
	let forbidden = $state(false);
	let saveError = $state('');
	let busy = $state(false);

	async function load(): Promise<void> {
		try {
			enabled = (await getClaudeCompaction()).enabled;
			loadError = '';
			forbidden = false;
		} catch (e) {
			if (isForbidden(e)) forbidden = true;
			else loadError = getErrorMessage(e, 'Could not read the compaction setting.');
		}
	}

	async function save(next: boolean): Promise<boolean> {
		if (busy) return false;
		busy = true;
		saveError = '';
		try {
			enabled = (await updateClaudeCompaction(next)).enabled;
			flashSaved();
			return true;
		} catch (e) {
			saveError = getErrorMessage(e, 'Could not save the compaction setting.');
			return false;
		} finally {
			busy = false;
		}
	}

	function reset() {
		const fallback = getEntry(ID)?.default;
		if (typeof fallback === 'boolean') void save(fallback);
	}

	onMount(() => void load());
</script>

<RowGroup title="Sessions">
	<SettingRow id={ID} value={enabled ?? undefined} onReset={reset}>
		{#if enabled !== null}
			<Switch checked={enabled} disabled={busy} onchange={save} />
		{:else if forbidden}
			<ValueText value={FORBIDDEN_TEXT} />
		{:else if loadError}
			<button type="button" class="st-btn sm" onclick={() => load()}>Try again</button>
		{:else}
			<ValueText value="Loading" />
		{/if}
	</SettingRow>
</RowGroup>
{#if loadError && enabled === null}<p class="pv-msg err" role="alert">{loadError}</p>{/if}
{#if saveError}<p class="pv-msg err" role="alert">{saveError}</p>{/if}
