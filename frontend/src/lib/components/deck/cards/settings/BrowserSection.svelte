<script lang="ts">
	/**
	 * Admin settings panel for "Claude in Chrome" (browser automation).
	 *
	 * Shows a live, structured status of the integration (chrome-doctor as a UI),
	 * an enable/disable toggle for the built-in, and a one-time "Set up browser"
	 * flow that opens an interactive screencast of the headless container Chrome so
	 * the user can sign the extension into their Claude account and pair — no
	 * desktop Chrome UI and no exposed DevTools port required.
	 */
	import { onMount } from 'svelte';
	import { Globe, RefreshCw } from 'lucide-svelte';
	import {
		getChromeMcpStatus,
		toggleChromeMcp,
		pairChromeMcp,
		type ChromeMcpStatus
	} from '$lib/api/chromeMcp';
	import ChromeSetupModal from '$lib/components/chrome/ChromeSetupModal.svelte';

	let status = $state<ChromeMcpStatus | null>(null);
	let loading = $state(true);
	let error = $state('');
	let pairMsg = $state('');
	let toggling = $state(false);
	let pairing = $state(false);
	let setupOpen = $state(false);

	async function load() {
		try {
			status = await getChromeMcpStatus();
			error = '';
		} catch (e: any) {
			error = e?.detail || 'Failed to load browser status';
		} finally {
			loading = false;
		}
	}

	async function onToggle() {
		if (!status || toggling) return;
		toggling = true;
		const next = !status.enabled;
		try {
			status = await toggleChromeMcp(next);
			error = '';
		} catch (e: any) {
			error = e?.detail || 'Failed to update setting';
		} finally {
			toggling = false;
		}
	}

	async function onPair() {
		if (!status || pairing) return;
		pairing = true;
		pairMsg = '';
		error = '';
		try {
			const res = await pairChromeMcp();
			status = res.status;
			pairMsg = res.message;
			if (!res.ok) error = res.message;
		} catch (e: any) {
			error = e?.detail || 'Failed to pair browser';
		} finally {
			pairing = false;
		}
	}

	onMount(load);

	// Checklist rows derived from status (label, ok, value text).
	type Check = { label: string; ok: boolean; note: string };
	let checks = $derived<Check[]>(
		status
			? [
					{ label: 'Chrome running', ok: status.chrome_running, note: status.chrome_running ? 'Running' : 'Stopped' },
					{
						label: 'DevTools port',
						ok: status.cdp_available,
						note: status.cdp_available
							? `Listening on 127.0.0.1:${status.cdp_port}`
							: status.cdp_enabled
								? 'Enabled — waiting for Chrome'
								: 'Disabled (set CHROME_CDP=1)'
					},
					{ label: 'Extension installed', ok: status.extension_installed, note: status.extension_installed ? 'Installed' : 'Not yet installed' },
					{ label: 'Native host', ok: status.native_host_present, note: status.native_host_present ? 'Present' : 'Missing' },
					{
						label: 'Signed in & connected',
						ok: status.connected,
						note: status.connected ? 'Extension is talking to the native host' : 'Not connected'
					},
					{
						label: 'Pinned to this browser',
						ok: status.paired,
						note: status.paired
							? `Pinned${status.paired_device_name ? `: ${status.paired_device_name}` : ''}`
							: 'Not pinned (auto-selects when only one browser is on the account)'
					}
				]
			: []
	);
</script>

<!-- Status & activation -->
<div class="set-group">
	<div class="set-grouphead">
		<h3><Globe />Claude in Chrome</h3>
		<p>
			Let the agent drive a real browser running inside this container. Sign in and pair once —
			it persists across restarts.
		</p>
	</div>

	{#if loading}
		<div class="set-row"><div class="lab"><p>Loading status…</p></div></div>
	{:else if !status}
		<div class="set-row"><div class="lab"><p class="msg err">{error || 'Status unavailable.'}</p></div></div>
	{:else}
		<!-- Enable toggle -->
		<div class="set-row">
			<div class="lab">
				<h4>Enable browser tools</h4>
				<p>Exposes the <code>claude-in-chrome</code> tools to every session.</p>
			</div>
			<div class="ctl">
				<button
					type="button"
					class="switch"
					class:on={status.enabled}
					role="switch"
					aria-checked={status.enabled}
					aria-label="Enable Claude in Chrome"
					disabled={toggling}
					onclick={onToggle}
				><i></i></button>
			</div>
		</div>

		<!-- Overall readiness + setup action -->
		<div class="set-row stack">
			<div class="lab">
				<h4>Setup &amp; status</h4>
				<p>{status.next_step}</p>
			</div>
			<div class="ctl">
				<div class="checks">
					{#each checks as c (c.label)}
						<div class="check">
							<span class="cdot" class:ok={c.ok}></span>
							<span class="clabel">{c.label}</span>
							<span class="cnote">{c.note}</span>
						</div>
					{/each}
				</div>
				<div class="setup-actions">
					{#if status.paired}
						<span class="b b-ok">Pinned</span>
					{:else if status.connected}
						<span class="b b-ok">Connected</span>
					{:else if status.ready}
						<span class="b b-ok">Ready</span>
					{:else}
						<span class="b b-warn">Setup needed</span>
					{/if}

					{#if status.connected && !status.paired}
						<!-- Working but not pinned: offer the one-click deterministic pin. -->
						<button
							type="button"
							class="ed-btn primary"
							onclick={onPair}
							disabled={pairing}
							title="Pin this container's browser so it stays selected even if another browser joins this account"
						>{pairing ? 'Pairing…' : 'Pair this browser'}</button>
					{/if}

					<button
						type="button"
						class="ed-btn"
						class:primary={!status.connected && !status.paired}
						onclick={() => (setupOpen = true)}
						disabled={!status.enabled}
						title={status.enabled ? 'Open the interactive browser setup' : 'Enable browser tools first'}
					>Set up browser</button>
					<button type="button" class="ed-btn sm" onclick={load} title="Refresh status">
						<RefreshCw size={14} />
					</button>
				</div>
				{#if pairMsg && !error}<p class="msg ok">{pairMsg}</p>{/if}
				{#if error}<p class="msg err">{error}</p>{/if}
			</div>
		</div>
	{/if}
</div>

<ChromeSetupModal
	open={setupOpen}
	onClose={() => {
		setupOpen = false;
		load();
	}}
	onStatus={(s) => (status = s)}
/>

<style>
	.checks {
		display: flex;
		flex-direction: column;
		gap: 6px;
		margin-bottom: 12px;
	}
	.check {
		display: flex;
		align-items: center;
		gap: 9px;
		font-size: 12.5px;
	}
	.cdot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		flex: none;
		background: var(--muted-foreground);
		opacity: 0.5;
	}
	.cdot.ok {
		background: var(--success, #16a34a);
		opacity: 1;
	}
	.clabel {
		color: var(--foreground);
		min-width: 160px;
	}
	.cnote {
		color: var(--muted-foreground);
	}
	.setup-actions {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}
	.b {
		font-size: 10px;
		font-weight: 600;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		padding: 2px 8px;
		border-radius: 999px;
	}
	.b-ok {
		color: var(--success, #16a34a);
		background: color-mix(in oklch, var(--success, #16a34a) 14%, transparent);
	}
	.b-warn {
		color: var(--warning, #b45309);
		background: color-mix(in oklch, var(--warning, #b45309) 16%, transparent);
	}
	.msg {
		margin: 8px 0 0;
		font-size: 12.5px;
	}
	.msg.err {
		color: var(--destructive);
	}
	.msg.ok {
		color: var(--success, #16a34a);
	}
	code {
		font-family: var(--font-mono, monospace);
		font-size: 12px;
	}
</style>
