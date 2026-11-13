<script lang="ts">
	/**
	 * Google's Antigravity agent (`agy`) on this worker: version and Check for
	 * update when installed, otherwise Install (the worker downloads Google's
	 * own release for its platform from the official manifest), with progress,
	 * results and errors. Also names peer workers that
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
	const sourceHint = $derived(status?.source === 'path' ? "Google's agent, found on this worker's PATH"
		: status?.source === 'override' ? "Google's agent, chosen by AI_SHUFFLE_ANTIGRAVITY_BIN"
		: "Google's agent on this worker");

	onMount(() => { if (isAdmin) void antigravity.load(); });
</script>

<RowGroup title="Antigravity agent" id="providers-antigravity-agent">
	{#if !isAdmin}
		<p class="pv-msg">The worker owner can install Antigravity here.</p>
	{:else if !status}
		<SettingRow label="Version"><ValueText mono value={antigravity.error ? 'Unknown' : 'Checking'} /></SettingRow>
	{:else if status.installed}
		<SettingRow label="Version" hint={sourceHint}>
			<ValueText mono status="ok" value={status.version ?? 'Installed'} />
		</SettingRow>
		<SettingRow label="Updates" hint="Asks Google for a newer release and installs it">
			<button type="button" class="st-btn" disabled={antigravity.installing || status.can_install === false}
				aria-busy={antigravity.installing && job?.kind === 'update'}
				onclick={() => antigravity.install('update')}>{antigravity.installing ? 'Checking' : 'Check for update'}</button>
		</SettingRow>
	{:else if antigravity.installing}
		<SettingRow label="Version" hint="Downloading Google's release">
			<button type="button" class="st-btn" disabled aria-busy="true">Installing</button>
		</SettingRow>
	{:else}
		<SettingRow label="Not installed" hint="Downloads Google's agent; nothing is bundled">
			<button type="button" class="st-btn primary" disabled={status.can_install === false}
				onclick={() => antigravity.install()}>Install Antigravity</button>
		</SettingRow>
	{/if}
</RowGroup>
{#if status && !status.installed && status.can_install === false && status.unsupported_message}
	<p class="pv-msg">{status.unsupported_message} Install Google's Antigravity CLI yourself so <code>agy</code> is on PATH.</p>
{/if}
{#if antigravity.installing}
	<p class="pv-msg" role="status">{job?.kind === 'update' ? 'Checking Google for a newer Antigravity' : 'Installing Antigravity'}</p>
{:else if job?.state === 'failed' && (job.message || job.error)}
	<p class="pv-msg err" role="alert">{job.message || job.error}</p>
{:else if job?.state === 'succeeded' && job.message && status?.installed}
	<p class="pv-msg" role="status">{job.message}</p>
{/if}
{#if peers.length}
	<p class="pv-msg err" role="alert">Update these workers before adding an Antigravity provider: {peers.join(', ')}.</p>
{/if}
{#if antigravity.error}
	<p class="pv-msg err" role="alert">{antigravity.error}</p>
	<button type="button" class="st-btn" onclick={() => antigravity.load(true)}>Check again</button>
{/if}
