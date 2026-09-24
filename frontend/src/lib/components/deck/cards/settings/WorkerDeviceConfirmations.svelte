<script lang="ts">
	import { onMount } from 'svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { isAdmin } from '$lib/stores/auth.svelte';
	import { confirmWorkerDevice, listWorkerDeviceConfirmations, type WorkerDeviceConfirmation } from '$lib/api/worker-device-confirmations';

	let devices = $state<WorkerDeviceConfirmation[]>([]);
	let loaded = $state(false);
	let loading = $state(false);
	let error = $state('');
	let notice = $state('');
	let saving = $state(false);
	let choice = $state<{ device: WorkerDeviceConfirmation; approved: boolean } | null>(null);
	let stopped = false;

	async function refresh() {
		if (loading || saving || stopped || !$isAdmin) return;
		loading = true;
		try {
			const next = await listWorkerDeviceConfirmations();
			if (stopped) return;
			devices = next;
			loaded = true;
			error = '';
			// Never apply a pending click to a replacement enrollment/credential.
			if (choice && !next.some((d) => d.device_id === choice?.device.device_id && d.paired_client_id === choice.device.paired_client_id && d.mobile_record_id === choice.device.mobile_record_id && d.confirmation === choice.device.confirmation && d.revoked_at === choice.device.revoked_at)) choice = null;
		} catch (e) {
			if (!stopped) error = getErrorMessage(e, 'Could not load worker device confirmations');
		} finally { loading = false; }
	}

	async function decide() {
		if (!choice || saving || error || !$isAdmin) return;
		const selected = choice;
		saving = true; notice = '';
		try {
			await confirmWorkerDevice(selected.device, selected.approved);
			if (stopped) return;
			choice = null;
			notice = selected.approved ? 'Device approved on this worker. Retry the connection on the device.' : 'Device denied on this worker. This identity cannot be approved again; pair a new identity to restore access.';
		} catch (e) {
			if (!stopped) { choice = null; error = getErrorMessage(e, 'Could not update worker device access'); }
		} finally { saving = false; }
		// Keep any failure visible until the owner explicitly refreshes.
		if (!error) await refresh();
	}

	onMount(() => {
		void refresh();
		const timer = setInterval(() => {
			if (document.visibilityState === 'visible' && !choice && !error) void refresh();
		}, 10_000);
		return () => { stopped = true; clearInterval(timer); };
	});

	function expired(device: WorkerDeviceConfirmation) {
		return Boolean(device.api_key_expires_at && Date.parse(device.api_key_expires_at) <= Date.now());
	}
</script>

{#if $isAdmin}
	<section class="worker-confirmations" aria-labelledby="worker-confirmations-heading">
		<div class="heading">
			<h4 id="worker-confirmations-heading">Account device access to this worker</h4>
			<button type="button" class="refresh" disabled={loading || saving} onclick={refresh}>{loading ? 'Refreshing…' : 'Refresh requests'}</button>
		</div>
		<p>Signing in to a Shuffle account does not grant this worker’s admin access. Approve here from the worker’s own browser session or an already-confirmed device. A LAN connection alone cannot approve.</p>
		<p class="warning">Only approve a request you initiated. Compare the device ID below with the ID shown on your device; account-supplied names are not proof of identity.</p>
		{#if error}
			<div role="alert">{error} Open this worker locally or use an already-confirmed owner device, then refresh before taking another action.</div>
		{/if}
		{#if notice}<p role="status">{notice}</p>{/if}
		{#if !loaded && !error}<p>Loading device requests…</p>{/if}
		{#if loaded && devices.length === 0}<p>No account device requests on this worker.</p>{/if}
		{#each devices as device (`${device.device_id}:${device.paired_client_id}`)}
			<article aria-label={device.device_name || 'Account device'}>
				<div class="device-heading"><strong>{device.device_name || 'Unnamed device'}</strong><span>{device.revoked_at ? 'Revoked' : device.confirmation === 'pending' ? 'Awaiting worker approval' : device.confirmation === 'approved' ? 'Approved' : 'Denied'}</span></div>
				<dl>
					<div><dt>Device ID</dt><dd><code>{device.hub_device_id || device.device_id}</code></dd></div>
					<div><dt>Platform</dt><dd>{device.platform || 'Unknown'}</dd></div>
					{#if device.api_key_expires_at}<div><dt>Key expires</dt><dd>{new Date(device.api_key_expires_at).toLocaleString()}{expired(device) ? ' (expired — pair again)' : ''}</dd></div>{/if}
				</dl>
				{#if device.confirmation === 'denied'}<p>This identity is denied. A fresh pairing with a new identity is required.</p>{/if}
				{#if !device.revoked_at && device.confirmation !== 'denied'}
					{#if choice?.device.device_id === device.device_id}
						<div class="decision">
							<p>{choice.approved ? 'Grant admin access to this worker only after matching the device ID.' : 'Deny access on this worker? This decision is permanent for this identity and stops existing sessions.'}</p>
							<div class="actions"><button type="button" disabled={saving || Boolean(error)} onclick={decide}>{saving ? 'Saving…' : choice.approved ? 'Confirm approval' : 'Confirm denial'}</button><button type="button" disabled={saving} onclick={() => { choice = null; }}>Cancel</button></div>
						</div>
					{:else}
						<div class="actions">
							{#if device.confirmation === 'pending'}<button type="button" disabled={saving || Boolean(error) || expired(device)} onclick={() => { notice = ''; choice = { device: { ...device }, approved: true }; }}>Approve {device.device_name || 'device'}</button>{/if}
							<button type="button" disabled={saving || Boolean(error)} onclick={() => { notice = ''; choice = { device: { ...device }, approved: false }; }}>{device.confirmation === 'approved' ? 'Revoke worker access' : 'Deny request'}</button>
						</div>
					{/if}
				{/if}
			</article>
		{/each}
	</section>
{/if}

<style>
	.worker-confirmations { display: grid; gap: 0.75rem; padding: 1rem; border: 1px solid var(--border); border-radius: 0.75rem; }
	.heading, .device-heading { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 0.5rem; }
	h4, p { margin: 0; }
	p, dl, span { font-size: 0.875rem; line-height: 1.5; }
	.warning, [role='alert'] { padding: 0.75rem; border-left: 3px solid var(--warning, orange); background: color-mix(in oklch, var(--warning, orange) 8%, var(--card)); }
	[role='alert'] { overflow-wrap: anywhere; }
	article { display: grid; gap: 0.75rem; border-top: 1px solid var(--border); padding-top: 0.75rem; min-width: 0; }
	dl { display: grid; gap: 0.25rem; margin: 0; }
	dl div { display: flex; flex-wrap: wrap; gap: 0.5rem; }
	dt { font-weight: 600; } dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
	.actions { display: flex; flex-wrap: wrap; gap: 0.5rem; }
	button { padding: 0.5rem 0.75rem; border: 1px solid var(--border); border-radius: 0.375rem; background: var(--secondary); color: var(--foreground); cursor: pointer; min-height: 40px; }
	button:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	button:hover:not(:disabled) { background: var(--muted); }
	button:disabled { opacity: 0.55; cursor: not-allowed; }
	.decision { display: grid; gap: 0.5rem; }
</style>
