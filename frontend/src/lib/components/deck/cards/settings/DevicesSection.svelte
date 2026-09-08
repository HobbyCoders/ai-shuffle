<script lang="ts">
	// Network membership and recovery remain separate from the unified
	// account/device list: independent workers do not share a database.
	import { onMount } from 'svelte';
	import { MonitorSmartphone, Server, Smartphone, Globe, Plus, Pencil, Trash2, Check, X, ShieldCheck, ShieldOff } from 'lucide-svelte';
	import { watchResources } from '$lib/services/watch-resources';
	import { getErrorMessage } from '$lib/api/client';
	import { appData } from '$lib/stores/app-data.svelte';
	import { listPairedClients, revokePairedClient, type PairedClient } from '$lib/api/pairing';
	import {
		listInstalls, renameInstall, deleteInstall, setInstallApproval,
		getAdvertiseStatus, setAdvertiseUrl, testAdvertiseReachability,
		type Install, type AdvertiseStatus, type AdvertiseReachability
	} from '$lib/api/installs';
	import PairingPanel from './PairingPanel.svelte';
	import NetworkPanel from './NetworkPanel.svelte';
	import AccountConnection from './AccountConnection.svelte';

	let installs = $state<Install[]>([]);
	let clients = $state<PairedClient[]>([]);
	let selfId = $state('');
	let workerName = $state('My worker');
	// Fail closed until the directory identifies the worker's enrollment mode.
	let networkEnrolled = $state(true);
	let workersLoaded = $state(false);
	let clientsLoaded = $state(false);
	let workersError = $state('');
	let clientsError = $state('');
	let advertiseError = $state('');
	let error = $state('');
	let success = $state('');
	let showPairing = $state(false);
	let pairingMounted = $state(false);
	let editingId = $state<string | null>(null);
	let editName = $state('');
	let removedDraft = $state<Install | null>(null);
	let draftNotice = $state('');
	let savingKey = $state<string | null>(null);
	let confirmation = $state<{ key: string; action: 'revoke' | 'remove' } | null>(null);
	let advertise = $state<AdvertiseStatus | null>(null);
	let advertiseInput = $state('');
	let savingAdvertise = $state(false);
	let testingAdvertise = $state(false);
	let reachability = $state<AdvertiseReachability | null>(null);
	let stopped = false;
	let workersVersion = 0;
	let clientsVersion = 0;
	let advertiseVersion = 0;
	let pendingWorkers = 0;
	let pendingClients = 0;
	let flashTimer: ReturnType<typeof setTimeout> | undefined;

	// Only API identity determines row identity: names and IDs can overlap across sources.
	type DeviceRow = { key: string; kind: 'worker'; device: Install; draft?: boolean }
		| { key: string; kind: 'client'; device: PairedClient };
	const devices = $derived<DeviceRow[]>([
		...installs.map((device) => ({ key: `worker:${device.id}`, kind: 'worker' as const, device })),
		...clients.map((device) => ({ key: `client:${device.id}`, kind: 'client' as const, device })),
		...(removedDraft ? [{ key: `worker:${removedDraft.id}`, kind: 'worker' as const, device: removedDraft, draft: true }] : [])
	]);
	const total = $derived(installs.length + clients.length);
	const complete = $derived(workersLoaded && clientsLoaded && !workersError && !clientsError);
	// Network-backed rows belong to NetworkPanel, never to legacy registry actions.

	onMount(() => {
		void load();
		// Pairing completion has no dedicated resource event. Refresh both sources while
		// this view is mounted, including when the enrollment panel has been collapsed.
		const poll = setInterval(() => {
			// Do not repeatedly supersede a slow response with another poll.
			if (!pendingWorkers) void loadWorkers();
			if (!pendingClients) void loadClients();
		}, 10_000);
		return () => {
			stopped = true; workersVersion++; clientsVersion++; advertiseVersion++;
			clearInterval(poll); clearTimeout(flashTimer);
		};
	});
	watchResources(['settings', 'network'], load);

	async function load() {
		await Promise.all([loadWorkers(), loadClients(), loadAdvertise()]);
	}

	async function loadWorkers() {
		if (savingKey?.startsWith('worker:')) return;
		const version = ++workersVersion;
		pendingWorkers++;
		const selected = installs.find((install) => `worker:${install.id}` === editingId) || removedDraft;
		const nameBefore = editName;
		try {
			const response = await listInstalls();
			if (stopped || version !== workersVersion) return;
			selfId = response.self_instance_id;
			workerName = response.installs.find(isSelf)?.device_name || 'My worker';
			networkEnrolled = response.installs.some((install) => !!install.device_id);
			installs = response.installs.filter((install) => !install.device_id);
			workersLoaded = true; workersError = '';
			if (selected && editingId === `worker:${selected.id}`) {
				const fresh = installs.find((install) => install.id === selected.id);
				const clean = editName === nameBefore && editName === selected.device_name;
				if (clean) {
					if (fresh) editName = fresh.device_name;
					else cancelEdit();
				} else if (!fresh) {
					removedDraft = selected;
					draftNotice = 'This device was removed elsewhere. Copy your unsaved name before closing it.';
				} else if (fresh.device_name !== selected.device_name) {
					draftNotice = 'This device changed elsewhere. Your unsaved name is kept here.';
				}
				if (fresh) removedDraft = null;
			}
		} catch (e) {
			if (!stopped && version === workersVersion) workersError = getErrorMessage(e, 'Could not load workers');
		} finally { pendingWorkers--; }
	}

	async function loadClients() {
		if (savingKey?.startsWith('client:')) return;
		const version = ++clientsVersion;
		pendingClients++;
		try {
			const response = await listPairedClients();
			if (stopped || version !== clientsVersion) return;
			clients = response; clientsLoaded = true; clientsError = '';
		} catch (e) {
			if (!stopped && version === clientsVersion) clientsError = getErrorMessage(e, 'Could not load phones and browsers');
		} finally { pendingClients--; }
	}

	async function loadAdvertise() {
		if (savingAdvertise) return;
		const version = ++advertiseVersion;
		const before = advertiseInput;
		const clean = advertiseInput === (advertise?.override || '');
		try {
			const next = await getAdvertiseStatus();
			if (stopped || version !== advertiseVersion) return;
			advertise = next; advertiseError = '';
			if (clean && advertiseInput === before) advertiseInput = next.override || '';
		} catch (e) {
			if (!stopped && version === advertiseVersion) advertiseError = getErrorMessage(e, 'Could not load connection settings');
		}
	}

	function isSelf(install: Install) { return install.id === selfId || install.is_self === true; }
	function clientKind(client: PairedClient) {
		// Older browser registrations used the default mobile client_type.
		if (/browser/i.test(client.platform) || ['browser', 'pwa'].includes(client.client_type)) return 'Browser';
		return client.client_type === 'mobile' ? 'Phone' : 'Device';
	}
	function flash(message: string) {
		clearTimeout(flashTimer); success = message;
		flashTimer = setTimeout(() => { success = ''; }, 4000);
	}
	function startEdit(install: Install) {
		editingId = `worker:${install.id}`; editName = install.device_name;
		confirmation = null; removedDraft = null; draftNotice = '';
	}
	function cancelEdit() { editingId = null; editName = ''; removedDraft = null; draftNotice = ''; }

	async function saveEdit(id: string) {
		const name = editName.trim();
		if (!name || savingKey || removedDraft) return;
		savingKey = `worker:${id}`; workersVersion++; error = '';
		try {
			const updated = await renameInstall(id, name);
			installs = installs.map((install) => install.id === id ? { ...install, ...updated } : install);
			cancelEdit(); flash('Device renamed');
		} catch (e) { error = getErrorMessage(e, 'Could not rename device'); }
		finally { savingKey = null; }
	}

	async function remove(install: Install) {
		if (savingKey || isSelf(install)) return;
		savingKey = `worker:${install.id}`; workersVersion++; error = '';
		try {
			await deleteInstall(install.id);
			installs = installs.filter((item) => item.id !== install.id);
			appData.dropDeviceSessions(install.id);
			confirmation = null; flash('Device removed');
		} catch (e) { error = getErrorMessage(e, 'Could not remove device'); }
		finally { savingKey = null; }
	}

	async function changeApproval(install: Install, approved: boolean) {
		if (savingKey || (!approved && isSelf(install))) return;
		savingKey = `worker:${install.id}`; workersVersion++; error = '';
		try {
			const updated = await setInstallApproval(install.id, approved);
			installs = installs.map((item) => item.id === install.id ? { ...item, ...updated } : item);
			confirmation = null;
			flash(approved ? (updated.restart_required ? 'Worker approved. Restart it to begin execution.' : 'Worker approved') : 'Worker access revoked');
		} catch (e) { error = getErrorMessage(e, approved ? 'Could not approve worker' : 'Could not revoke worker'); }
		finally { savingKey = null; }
	}

	async function revokeClient(client: PairedClient) {
		if (savingKey) return;
		savingKey = `client:${client.id}`; clientsVersion++; error = '';
		try {
			await revokePairedClient(client.id);
			clients = clients.filter((item) => item.id !== client.id);
			confirmation = null; flash('Device access revoked');
		} catch (e) { error = getErrorMessage(e, 'Could not revoke device'); }
		finally { savingKey = null; }
	}

	async function saveAdvertise() {
		if (!advertise || savingAdvertise || advertise.env_pinned) return;
		const url = advertiseInput.trim();
		if (url && !/^https?:\/\//.test(url)) { error = 'URL must start with http:// or https://'; return; }
		savingAdvertise = true; advertiseVersion++; error = '';
		try {
			advertise = await setAdvertiseUrl(url);
			advertiseInput = advertise.override || '';
			reachability = null;
			flash(url ? 'Address saved' : 'Using the auto-detected address');
		} catch (e) { error = getErrorMessage(e, 'Could not save address'); }
		finally { savingAdvertise = false; }
	}
	async function testAdvertise() {
		testingAdvertise = true; error = '';
		try { reachability = await testAdvertiseReachability(); }
		catch (e) { error = getErrorMessage(e, 'Could not test address'); }
		finally { testingAdvertise = false; }
	}
	function seen(input: string | null): string {
		if (!input) return 'Not seen yet';
		const timestamp = new Date(input).getTime();
		if (!Number.isFinite(timestamp)) return 'Last seen unknown';
		const minutes = Math.max(0, Math.floor((Date.now() - timestamp) / 60_000));
		if (minutes < 1) return 'Seen just now';
		if (minutes < 60) return `Seen ${minutes}m ago`;
		if (minutes < 1440) return `Seen ${Math.floor(minutes / 60)}h ago`;
		return `Seen ${Math.floor(minutes / 1440)}d ago`;
	}
</script>

<NetworkPanel />

<section class="devices-section" aria-labelledby="devices-heading">
	<header class="devices-header">
		<div class="heading"><h3 id="devices-heading"><MonitorSmartphone size={18} /> Devices</h3>
			<span class="device-count">{#if complete}{total} {total === 1 ? 'device' : 'devices'}{:else if workersLoaded || clientsLoaded || workersError || clientsError}{total} loaded{:else}Loading…{/if}</span>
		</div>
		<button type="button" class="device-button add-button" aria-expanded={showPairing} aria-controls="device-enrollment" onclick={() => { pairingMounted = true; showPairing = !showPairing; }}>
			{#if showPairing}<X size={15} /> Close{:else}<Plus size={15} /> Add device{/if}
		</button>
	</header>
	<AccountConnection {workerName} />
	<div id="device-enrollment" hidden={!showPairing}>{#if pairingMounted}<PairingPanel allowWorkerEnrollment={workersLoaded && !networkEnrolled} />{/if}</div>

	{#if workersError}<div class="load-error"><p role="alert">Workers: {workersError}</p><button type="button" class="device-button" onclick={loadWorkers}>Retry workers</button></div>{/if}
	{#if clientsError}<div class="load-error"><p role="alert">Phones and browsers: {clientsError}</p><button type="button" class="device-button" onclick={loadClients}>Retry phones and browsers</button></div>{/if}
	{#if !workersLoaded && !workersError}<p class="list-message" role="status">Loading workers…</p>{/if}
	{#if !clientsLoaded && !clientsError}<p class="list-message" role="status">Loading phones and browsers…</p>{/if}

	{#if devices.length}
		<ul class="device-list" aria-label="Devices">
			{#each devices as item (item.key)}
				<li class="device-row" data-device-key={item.key}>
					<span class="device-icon" aria-hidden="true">{#if item.kind === 'worker'}<Server size={18} />{:else if clientKind(item.device) === 'Phone'}<Smartphone size={18} />{:else}<Globe size={18} />{/if}</span>
					<div class="device-main">
						{#if item.kind === 'worker' && editingId === item.key}
							<div class="name-editor">
								<input class="device-input" aria-label="Device name" bind:value={editName} maxlength="60" disabled={savingKey !== null} onkeydown={(event) => { if (event.key === 'Enter') void saveEdit(item.device.id); if (event.key === 'Escape' && !savingKey) cancelEdit(); }} />
								<button type="button" class="device-button icon-button" aria-label="Save name" title="Save name" disabled={savingKey !== null || !editName.trim() || !!removedDraft} onclick={() => saveEdit(item.device.id)}><Check size={15} /></button>
								<button type="button" class="device-button icon-button" aria-label="Cancel rename" title="Cancel rename" disabled={savingKey !== null} onclick={cancelEdit}><X size={15} /></button>
							</div>
						{:else}
							<div class="name-line"><span class="device-name">{item.device.device_name}</span>{#if item.kind === 'worker' && isSelf(item.device)}<span class="self-label">This device</span>{/if}</div>
						{/if}
						<div class="device-meta">
							<span>{item.kind === 'worker' ? 'Worker' : clientKind(item.device)}</span>
							{#if item.device.platform && !/^(browser|pwa|mobile)$/i.test(item.device.platform)}<span>{item.device.platform.replace(/-browser$/i, '')}</span>{/if}
							{#if item.kind === 'worker'}
								{#if item.draft}<span>Removed elsewhere</span>{:else if item.device.approved === false}<span class="approval-needed">Approval needed</span>{:else}<span>{item.device.online ? 'Online' : seen(item.device.last_seen)}</span>{/if}
							{:else}<span>{seen(item.device.last_seen)}</span>{/if}
						</div>
					</div>
					{#if editingId !== item.key && confirmation?.key !== item.key}
						<div class="device-actions">
							{#if item.kind === 'worker'}
								{@const install = item.device}
								{#if install.approved === false}
									<button type="button" class="device-button" aria-label={`Approve ${install.device_name}`} disabled={savingKey !== null} onclick={() => changeApproval(install, true)}><ShieldCheck size={15} /> Approve</button>
								{:else}
									<button type="button" class="device-button icon-button" aria-label={`Rename ${install.device_name}`} title="Rename device" disabled={savingKey !== null} onclick={() => startEdit(install)}><Pencil size={15} /></button>
								{/if}
								{#if !isSelf(install)}
									{#if install.approved !== false}<button type="button" class="device-button icon-button" aria-label={`Revoke ${install.device_name}`} title="Revoke worker access" disabled={savingKey !== null} onclick={() => { confirmation = { key: item.key, action: 'revoke' }; }}><ShieldOff size={15} /></button>{/if}
									<button type="button" class="device-button icon-button" aria-label={`Remove ${install.device_name}`} title="Remove device" disabled={savingKey !== null} onclick={() => { confirmation = { key: item.key, action: 'remove' }; }}><Trash2 size={15} /></button>
								{/if}
							{:else}
								<button type="button" class="device-button icon-button" aria-label={`Revoke ${item.device.device_name}`} title="Revoke device access" disabled={savingKey !== null} onclick={() => { confirmation = { key: item.key, action: 'revoke' }; }}><ShieldOff size={15} /></button>
							{/if}
						</div>
					{/if}
					{#if confirmation?.key === item.key}
						<div class="confirmation">
							<p>{confirmation.action === 'remove' ? 'Remove this device from the list? It may reappear if it reconnects.' : item.kind === 'worker' ? 'Revoke access? This worker will no longer receive new tasks.' : 'Revoke access? This device will need to pair again.'}</p>
							<div class="confirmation-actions">
								<button type="button" class="device-button danger" disabled={savingKey !== null} onclick={() => { if (item.kind === 'client') void revokeClient(item.device); else if (confirmation?.action === 'remove') void remove(item.device); else void changeApproval(item.device, false); }}>{confirmation.action === 'remove' ? 'Confirm remove' : 'Confirm revoke'}</button>
								<button type="button" class="device-button" disabled={savingKey !== null} onclick={() => { confirmation = null; }}>Cancel</button>
							</div>
						</div>
					{/if}
				</li>
			{/each}
		</ul>
	{:else if complete}<p class="list-message">No devices yet. Add a device to get started.</p>{/if}
	{#if draftNotice}<p class="list-message" role="status">{draftNotice}</p>{/if}
	{#if error}<p class="feedback error" role="alert">{error}</p>{/if}
	{#if success}<p class="feedback" role="status">{success}</p>{/if}
	{#if advertiseError}<div class="load-error"><p role="alert">Connection settings: {advertiseError}</p><button type="button" class="device-button" onclick={loadAdvertise}>Retry connection settings</button></div>{/if}

	<details class="connection-settings">
		<summary>Connection settings</summary>
		<div class="address-settings">
			<label for="device-address">This worker's address</label>
			<p id="device-address-help">Used to open this worker's files from other devices. {#if advertise?.env_pinned}Set by CLUSTER_ADVERTISE_URL.{:else}Leave blank to detect automatically.{/if}</p>
			<div class="address-controls">
				<input id="device-address" class="device-input" type="url" bind:value={advertiseInput} placeholder={advertise?.url || 'https://worker.example.com'} aria-describedby="device-address-help" disabled={!advertise || advertise.env_pinned || savingAdvertise} autocomplete="off" />
				<button type="button" class="device-button" disabled={testingAdvertise || savingAdvertise || !advertise?.url} onclick={testAdvertise}>{testingAdvertise ? 'Testing…' : 'Test address'}</button>
				<button type="button" class="device-button" disabled={!advertise || advertise.env_pinned || savingAdvertise || advertiseInput.trim() === (advertise.override || '')} onclick={saveAdvertise}>{savingAdvertise ? 'Saving…' : 'Save address'}</button>
			</div>
			{#if reachability}<p class:error={!reachability.ok} role="status">{reachability.detail}</p>{/if}
		</div>
	</details>
</section>

<style>
	.devices-section { border: 1px solid var(--border); border-radius: var(--radius); background: var(--card); color: var(--foreground); min-width: 0; }
	.devices-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 18px; border-bottom: 1px solid var(--border); }
	.heading { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 12px; min-width: 0; }
	h3 { display: flex; align-items: center; gap: 8px; margin: 0; font-family: var(--font-display); font-size: 18px; font-weight: 600; letter-spacing: -.02em; }
	.device-count { color: var(--muted-foreground); font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
	.device-button { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 40px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; background: transparent; color: var(--foreground); font: inherit; font-size: 12px; line-height: 1.4; cursor: pointer; }
	.device-button:hover:not(:disabled) { background: var(--hover-overlay); }
	.device-button:disabled { opacity: .5; cursor: default; }
	.device-button:focus-visible, .device-input:focus-visible, summary:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.add-button { flex-shrink: 0; color: var(--primary); }
	.icon-button { width: 40px; padding: 0; border-color: transparent; color: var(--muted-foreground); flex-shrink: 0; }
	.device-button.danger, .error { color: var(--destructive); }
	.device-list { list-style: none; margin: 0; padding: 0 18px; }
	.device-row { display: grid; grid-template-columns: 22px minmax(0, 1fr) auto; align-items: center; gap: 8px 10px; padding: 14px 0; }
	.device-row + .device-row { border-top: 1px solid var(--border); }
	.device-icon { display: flex; color: var(--muted-foreground); }
	.device-main { min-width: 0; }
	.name-line { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 8px; }
	.device-name { font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
	.self-label { font-size: 11px; color: var(--primary); white-space: nowrap; }
	.device-meta { display: flex; flex-wrap: wrap; gap: 2px 8px; margin-top: 3px; font-size: 11px; line-height: 1.5; color: var(--muted-foreground); }
	.device-meta span { overflow-wrap: anywhere; }
	.device-meta span + span::before { content: '·'; margin-right: 8px; }
	.approval-needed { color: var(--warning-foreground, var(--warning)); }
	.device-actions, .confirmation-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }
	.device-input { box-sizing: border-box; min-width: 0; min-height: 40px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--background); color: var(--foreground); font: inherit; font-size: 13px; }
	.device-input:disabled { opacity: .55; }
	.name-editor { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }
	.name-editor .device-input { flex: 1 1 120px; width: 100%; }
	.confirmation { grid-column: 2 / -1; }
	.confirmation p, .load-error p, .address-settings p { margin: 0 0 8px; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
	.list-message, .feedback { margin: 0; padding: 12px 18px; font-size: 12px; line-height: 1.5; color: var(--muted-foreground); }
	.feedback { color: var(--primary); }
	.feedback.error { color: var(--destructive); }
	.load-error { padding: 12px 18px; color: var(--destructive); }
	.connection-settings { padding: 0 18px; border-top: 1px solid var(--border); }
	summary { padding: 14px 0; font-size: 12px; color: var(--muted-foreground); cursor: pointer; min-height: 44px; box-sizing: border-box; }
	.address-settings { padding: 0 0 16px; }
	.address-settings label { font-size: 13px; font-weight: 600; }
	.address-settings p { margin-top: 6px; color: var(--muted-foreground); }
	.address-settings p.error { color: var(--destructive); }
	.address-controls { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
	.address-controls .device-input { flex: 1 1 220px; width: 100%; }
	@media (max-width: 560px) {
		.devices-header { padding: 12px; }
		.heading { flex-direction: column; align-items: flex-start; }
		.device-list { padding: 0 12px; }
		.device-row { grid-template-columns: 20px minmax(0, 1fr); gap: 6px 8px; }
		.device-actions { grid-column: 2; justify-content: flex-end; }
		.device-button, .device-input { min-height: 44px; }
		.icon-button { width: 44px; }
		.connection-settings { padding: 0 12px; }
		.load-error, .list-message, .feedback { padding: 12px; }
	}
</style>
