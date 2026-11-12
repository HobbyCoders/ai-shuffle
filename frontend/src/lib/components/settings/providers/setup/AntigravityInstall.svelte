<script lang="ts">
	/**
	 * Google's Antigravity agent (`agy`) on this worker: version when installed,
	 * otherwise Install (the worker downloads Google's own release from its
	 * official manifest) with progress and errors. Also names peer workers that
	 * must update before an Antigravity provider can be added.
	 */
	import { onMount } from 'svelte';
	import '../providers.css';
	import RowGroup from '../../rows/RowGroup.svelte';
	import SettingRow from '../../rows/SettingRow.svelte';
	import ValueText from '../../rows/ValueText.svelte';
	import { antigravity } from '../antigravity.svelte';

	let { isAdmin = true }: { isAdmin?: boolean } = $props();

	const status = $derived(antigravity.status);
	const job = $derived(status?.install);
	const peers = $derived(status?.workers_requiring_update ?? []);

	onMount(() => { if (isAdmin) void antigravity.load(); });
</script>

<RowGroup title="Antigravity agent" id="providers-antigravity-agent">
	{#if !isAdmin}
		<p class="pv-msg">The worker owner can install Antigravity here.</p>
	{:else if !status}
		<SettingRow label="Version"><ValueText mono value={antigravity.error ? 'Unknown' : 'Checking'} /></SettingRow>
	{:else if status.installed}
		<SettingRow label="Version" hint="Google's agent on this worker">
			<ValueText mono status="ok" value={status.version ?? 'Installed'} />
		</SettingRow>
	{:else if antigravity.installing}
		<SettingRow label="Version" hint="Downloading Google's release">
			<button type="button" class="st-btn" disabled aria-busy="true">Installing</button>
		</SettingRow>
		<span class="sr-only" role="status">Installing Antigravity</span>
	{:else}
		<SettingRow label="Not installed" hint="Downloads Google's agent; nothing is bundled">
			<button type="button" class="st-btn primary" onclick={() => antigravity.install()}>Install Antigravity</button>
		</SettingRow>
	{/if}
</RowGroup>
{#if job?.state === 'failed' && job.error && !status?.installed}
	<p class="pv-msg err" role="alert">{job.error}</p>
{/if}
{#if peers.length}
	<p class="pv-msg err" role="alert">Update these workers before adding an Antigravity provider: {peers.join(', ')}.</p>
{/if}
{#if antigravity.error}
	<p class="pv-msg err" role="alert">{antigravity.error}</p>
	<button type="button" class="st-btn" onclick={() => antigravity.load(true)}>Check again</button>
{/if}
