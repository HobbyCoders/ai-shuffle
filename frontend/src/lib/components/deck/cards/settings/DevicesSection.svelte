<script lang="ts">
	/**
	 * DevicesSection — Settings → Devices.
	 *
	 * One place for everything that connects to this worker: the Shuffle
	 * account it belongs to, the other workers in its network, the phones and
	 * browsers paired with it, and how to add more. Invites and pairing codes
	 * admit a device on their own; a device that signs in to the same account
	 * is admitted by whichever worker is online. Nothing waits for approval,
	 * and the account always wins: a worker that signs in after a clean
	 * install switches to the account's settings by itself.
	 */
	import { onMount } from 'svelte';
	import { AlertTriangle, Check, Globe, KeyRound, Loader2, MonitorSmartphone, Pencil, Plus, Server, ShieldCheck, ShieldOff, Smartphone, Trash2, X } from 'lucide-svelte';
	import { watchResources } from '$lib/services/watch-resources';
	import { getErrorMessage } from '$lib/api/client';
	import { isAdmin } from '$lib/stores/auth.svelte';
	import { appData } from '$lib/stores/app-data.svelte';
	import { listPairedClients, revokePairedClient, type PairedClient } from '$lib/api/pairing';
	import { revokeAccountRegistration, type AccountProfile } from '$lib/api/account';
	import { accountRegistrations, type AccountDeviceRow } from './account-devices';
	import { listInstalls, renameInstall, deleteInstall, setInstallApproval, type Install } from '$lib/api/installs';
	import {
		adoptAccountNetwork, createNetwork, getNetworkStatus, renameNetworkMember, revokeNetworkMember, unlockWithRecoveryKey, setCloudBackup,
		type NetworkMember, type NetworkStatus
	} from '$lib/api/network';
	import { NETWORK_STATE_HINTS, NETWORK_STATE_LABELS, deriveNetworkState, relativeTime } from '$lib/api/network-status';
	import AccountPanel from './AccountPanel.svelte';
	import AddDevicePanel from './AddDevicePanel.svelte';
	import JoinNetworkPanel from './JoinNetworkPanel.svelte';

	const STATUS_MS = 5000;
	const JOINING_MS = 3000;
	const CLIENTS_MS = 10_000;

	// ── Data ────────────────────────────────────────────────────────────────
	let status = $state<NetworkStatus | null>(null);
	let savingBackup = $state(false);
	async function toggleBackup() {
		if (!status) return;
		savingBackup = true;
		try { status = { ...status, hub: await setCloudBackup(!status.hub?.backup_enabled) }; }
		catch (e) { error = getErrorMessage(e, 'Could not change cloud backup'); }
		finally { savingBackup = false; }
	}
	let accountProfile = $state<AccountProfile | null>(null);
	let accountPanel: AccountPanel;
	let accountInventoryReady = $state(false);
	let stale = $state(false);
	let statusError = $state('');
	let installs = $state<Install[]>([]);
	let selfInstallId = $state('');
	let workersLoaded = $state(false);
	let workersError = $state('');
	let clients = $state<PairedClient[]>([]);
	let clientsLoaded = $state(false);
	let clientsError = $state('');
	let error = $state('');
	let success = $state('');
	let now = $state(Date.now());

	// ── UI ──────────────────────────────────────────────────────────────────
	let showAdd = $state(false);
	let addMounted = $state(false);
	let editing = $state<string | null>(null);
	let editName = $state('');
	let saving = $state<string | null>(null);
	let confirmation = $state<{ key: string; action: 'revoke' | 'remove' } | null>(null);
	let revokedNotice = $state<string | null>(null);
	let adopting = $state(false);
	let startingFresh = $state(false);
	let recoveryInput = $state('');
	let unlocking = $state(false);
	let stopped = false;
	let statusTimer: ReturnType<typeof setTimeout> | null = null;
	let clientsTimer: ReturnType<typeof setInterval> | null = null;
	let pendingClients = 0;
	let pendingWorkers = 0;
	let flashTimer: ReturnType<typeof setTimeout> | undefined;

	// ── Derived ─────────────────────────────────────────────────────────────
	const uiState = $derived(deriveNetworkState(status, stale));
	const enrolled = $derived(Boolean(status?.enrolled));
	const self = $derived(status?.members?.find((m) => m.is_self) ?? null);
	const members = $derived((status?.members ?? []).filter((m) => m.status === 'active' && !m.is_self));
	const legacyInstalls = $derived(installs.filter((install) => !install.device_id && install.id !== selfInstallId && !install.is_self));
	const joining = $derived([
		...(status?.pending_joins ?? []).map((join) => ({ key: `join:${join.id}`, name: join.name, platform: join.platform, via: 'invite' as const })),
		...(status?.hub?.pending_devices ?? []).map((device) => ({ key: `hub:${device.device_id}`, name: device.name || device.device_id, platform: device.platform, via: 'account' as const }))
	]);
	const workerName = $derived(self?.name ?? installs.find((install) => install.is_self || install.id === selfInstallId)?.device_name ?? 'My worker');
	const registrations = $derived(accountRegistrations(accountProfile, status, installs, clients));
	const accountDevices = $derived(registrations.filter((device) => !device.localKey));
	const workerCount = $derived((status ? 1 : 0) + members.length + legacyInstalls.length);
	const total = $derived(workerCount + clients.length + accountDevices.length);
	const complete = $derived(workersLoaded && clientsLoaded && accountInventoryReady && !workersError && !clientsError && status !== null);
	const enrollmentPending = $derived(!enrolled && (status?.enrollment?.status === 'pending' || status?.enrollment?.status === 'syncing'));
	const waitingOnAccount = $derived(!enrolled && !status?.enrollment && Boolean(status?.hub?.linked) && status?.hub?.status === 'pending');
	const switchingToAccount = $derived(enrolled && Boolean(status?.hub?.linked) && status?.hub?.status === 'conflict');
	const accountWorkers = $derived((status?.hub?.account_network?.devices ?? []).filter((d) => d.status === 'active' && d.kind !== 'consumer'));
	const recoveryAvailable = $derived(Boolean(status?.hub?.account_network?.recovery_available));

	// ── Loading ─────────────────────────────────────────────────────────────
	onMount(() => {
		void loadAll();
		scheduleStatus();
		clientsTimer = setInterval(() => {
			if (!pendingClients) void loadClients();
			if (!pendingWorkers) void loadWorkers();
			now = Date.now();
		}, CLIENTS_MS);
		return () => {
			stopped = true;
			if (statusTimer) clearTimeout(statusTimer);
			if (clientsTimer) clearInterval(clientsTimer);
			clearTimeout(flashTimer);
		};
	});
	watchResources(['settings', 'network'], loadAll);

	function scheduleStatus() {
		if (stopped) return;
		if (statusTimer) clearTimeout(statusTimer);
		statusTimer = setTimeout(async () => {
			if (typeof document === 'undefined' || document.visibilityState === 'visible') await loadStatus();
			scheduleStatus();
		}, enrollmentPending || waitingOnAccount || switchingToAccount || joining.length ? JOINING_MS : STATUS_MS);
	}

	async function loadAll() {
		await Promise.all([loadStatus(), loadWorkers(), loadClients()]);
	}

	async function loadStatus() {
		try {
			const next = await getNetworkStatus();
			if (stopped) return;
			const wasJoining = status !== null && !status.enrolled && Boolean(status.enrollment);
			status = next; stale = false; statusError = '';
			if (wasJoining && next.enrolled) flash(`Joined ${next.network?.name || 'the network'}`);
			if (editing === 'self' && !editName) editName = next.members?.find((m) => m.is_self)?.name ?? '';
		} catch (e) {
			if (stopped) return;
			stale = true;
			if (status === null) statusError = getErrorMessage(e, 'Could not load the worker network');
		}
	}

	async function loadWorkers() {
		if (saving?.startsWith('legacy:')) return;
		pendingWorkers++;
		try {
			const response = await listInstalls();
			if (stopped) return;
			selfInstallId = response.self_instance_id;
			installs = response.installs;
			workersLoaded = true; workersError = '';
		} catch (e) {
			if (!stopped) workersError = getErrorMessage(e, 'Could not load workers');
		} finally { pendingWorkers--; }
	}

	async function loadClients() {
		if (saving?.startsWith('client:')) return;
		pendingClients++;
		try {
			const response = await listPairedClients();
			if (stopped) return;
			clients = response; clientsLoaded = true; clientsError = '';
		} catch (e) {
			if (!stopped) clientsError = getErrorMessage(e, 'Could not load phones and browsers');
		} finally { pendingClients--; }
	}

	function flash(message: string) {
		clearTimeout(flashTimer); success = message;
		flashTimer = setTimeout(() => { success = ''; }, 4000);
	}

	function clientKind(client: PairedClient): 'Browser' | 'Phone' | 'Device' {
		if (/browser/i.test(client.platform) || ['browser', 'pwa'].includes(client.client_type)) return 'Browser';
		return client.client_type === 'mobile' ? 'Phone' : 'Device';
	}

	function seen(input: string | null | undefined): string {
		if (!input) return 'Not seen yet';
		return `Seen ${relativeTime(input, now)}`;
	}

	// ── Actions ─────────────────────────────────────────────────────────────
	function startEdit(key: string, name: string) { editing = key; editName = name; confirmation = null; }
	function cancelEdit() { editing = null; editName = ''; }

	async function saveEdit() {
		const name = editName.trim();
		if (!name || saving || !editing) return;
		const key = editing;
		saving = key; error = '';
		try {
			if (key === 'self' && self) {
				await renameNetworkMember(self.device_id, name);
			} else if (key.startsWith('legacy:')) {
				const id = key.slice('legacy:'.length);
				const updated = await renameInstall(id, name);
				installs = installs.map((install) => install.id === id ? { ...install, ...updated } : install);
			} else if (key.startsWith('member:')) {
				await renameNetworkMember(key.slice('member:'.length), name);
			}
			cancelEdit(); flash('Device renamed');
			await loadStatus();
		} catch (e) { error = getErrorMessage(e, 'Could not rename the device'); }
		finally { saving = null; }
	}

	async function revokeAccess(key: string, name: string, action: 'revoke' | 'remove' = 'revoke') {
		if (saving) return;
		const accounts = registrations.filter((device) => device.key === key || device.localKey === key);
		if (accounts.some((device) => device.current)) return;
		saving = key; error = '';
		let accountRevoked = false;
		let cleanupPending = false;
		try {
			// Revoke account authority first so it cannot readmit the local device.
			for (const device of accounts) {
				const result = await revokeAccountRegistration(device.registration, device.id);
				if (!result.ok) throw new Error('Account revocation was not confirmed');
				accountRevoked = true;
				cleanupPending ||= Boolean(result.cleanup_pending);
				if (accountProfile) accountProfile = {
					...accountProfile,
					servers: accountProfile.servers.filter((worker) => device.registration !== 'servers' || worker.id !== device.id),
					devices: accountProfile.devices?.filter((item) => device.registration !== 'devices' || item.id !== device.id)
				};
			}
			if (key.startsWith('client:')) {
				const id = key.slice('client:'.length);
				await revokePairedClient(id);
				clients = clients.filter((item) => item.id !== id);
			} else if (key.startsWith('member:')) {
				const result = await revokeNetworkMember(key.slice('member:'.length));
				revokedNotice = result.rotate_provider_keys_recommended ? name : null;
				await loadStatus();
			} else if (key.startsWith('legacy:')) {
				const id = key.slice('legacy:'.length);
				if (action === 'remove') {
					await deleteInstall(id);
					installs = installs.filter((item) => item.id !== id);
					appData.dropDeviceSessions(id);
				} else {
					const updated = await setInstallApproval(id, false);
					installs = installs.map((item) => item.id === id ? { ...item, ...updated } : item);
				}
			}
			confirmation = null;
			flash(`${name} access revoked`);
			if (cleanupPending) error = 'Access was revoked, but remote connection cleanup is still pending.';
		} catch (e) {
			error = getErrorMessage(e, 'Could not revoke device access');
			if (accountRevoked) error += ' Account access was revoked. Retry to finish removing the remaining access.';
		} finally {
			if (accountRevoked) await accountPanel.loadProfile(true);
			saving = null;
		}
	}

	async function approveLegacy(install: Install, approved: boolean) {
		if (saving) return;
		saving = `legacy:${install.id}`; error = '';
		try {
			const updated = await setInstallApproval(install.id, approved);
			installs = installs.map((item) => item.id === install.id ? { ...item, ...updated } : item);
			confirmation = null;
			flash(approved ? 'Worker approved' : 'Worker access revoked');
		} catch (e) { error = getErrorMessage(e, approved ? 'Could not approve the worker' : 'Could not revoke the worker'); }
		finally { saving = null; }
	}

	async function startFresh() {
		startingFresh = true; error = '';
		try { await createNetwork(); flash('This worker now keeps its own settings'); await loadStatus(); }
		catch (e) { error = getErrorMessage(e, 'Could not set up this worker'); }
		finally { startingFresh = false; }
	}

	async function adopt() {
		adopting = true; error = '';
		try {
			await adoptAccountNetwork();
			flash('Switching to your account\'s settings');
			await loadStatus(); scheduleStatus();
		} catch (e) { error = getErrorMessage(e, 'Could not switch to the account\'s settings'); }
		finally { adopting = false; }
	}

	async function unlock() {
		unlocking = true; error = '';
		try {
			const result = await unlockWithRecoveryKey(recoveryInput);
			recoveryInput = '';
			flash(`Restored ${result.network?.name || 'your settings'}`);
			await loadStatus();
		} catch (e) { error = getErrorMessage(e, 'The recovery key was not accepted'); }
		finally { unlocking = false; }
	}

	function openProviders() {
		document.getElementById('providers-section')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
	}
</script>

{#snippet accountControl(device: AccountDeviceRow)}
	{#if $isAdmin && !device.current}
		{#if confirmation?.key !== device.key}
			<div class="actions"><button type="button" class="btn icon" aria-label="Revoke {device.name} from account" title="Revoke account access" disabled={saving !== null} onclick={() => { confirmation = { key: device.key, action: 'revoke' }; }}><ShieldOff size={15} /></button></div>
		{:else}
			<div class="confirm">
				<p>Revoke {device.name} from your account? {device.kind === 'Worker' ? 'This worker will lose account access and its remote connection.' : 'This device will need to sign in again.'}</p>
				<div class="row">
					<button type="button" class="btn sm danger" disabled={saving !== null} onclick={() => revokeAccess(device.key, device.name)}>{saving === device.key ? 'Revoking…' : 'Confirm revoke'}</button>
					<button type="button" class="btn sm" disabled={saving !== null} onclick={() => { confirmation = null; }}>Cancel</button>
				</div>
			</div>
		{/if}
	{/if}
{/snippet}

<section class="devices" aria-labelledby="devices-heading">
	<header class="head">
		<div class="heading">
			<h3 id="devices-heading"><MonitorSmartphone size={18} /> Devices</h3>
			<span class="count">{#if complete}{total} {total === 1 ? 'device' : 'devices'}{:else if workersLoaded || clientsLoaded || workersError || clientsError}{total} loaded{:else}Loading…{/if}</span>
			<span class="pill s-{uiState}" title={NETWORK_STATE_HINTS[uiState]}>{NETWORK_STATE_LABELS[uiState]}</span>
		</div>
		{#if $isAdmin}
			<button type="button" class="btn add" aria-expanded={showAdd} aria-controls="device-enrollment" onclick={() => { addMounted = true; showAdd = !showAdd; }}>
				{#if showAdd}<X size={15} /> Close{:else}<Plus size={15} /> Add device{/if}
			</button>
		{/if}
	</header>

	<AccountPanel bind:this={accountPanel} {workerName} bind:profile={accountProfile} bind:inventoryReady={accountInventoryReady} />

	<div id="device-enrollment" hidden={!showAdd}>{#if addMounted}<AddDevicePanel {enrolled} allowLegacyRunner={workersLoaded && !enrolled && installs.length > 0} />{/if}</div>

	<!-- Situations that need a word: joining, waiting for the account, switching to the account, a fresh worker. -->
	{#if statusError}
		<div class="notice error-notice"><p role="alert">{statusError}</p><button type="button" class="btn sm" onclick={loadStatus}>Retry</button></div>
	{:else if status && !enrolled}
		{#if enrollmentPending}
			<div class="notice"><JoinNetworkPanel {status} onChanged={loadStatus} /></div>
		{:else if waitingOnAccount}
			<div class="notice" role="status">
				<h4><Loader2 size={14} class="spin" /> Joining your account's network</h4>
				{#if accountWorkers.length}
					<p>Your account already syncs settings between {accountWorkers.map((d) => d.name || 'a worker').join(', ')}. This worker is admitted as soon as one of them is online.</p>
				{:else}
					<p>This worker is admitted as soon as another worker on your account is online.</p>
				{/if}
				{#if recoveryAvailable && $isAdmin}
					<details>
						<summary><KeyRound size={13} /> No other worker available? Use your recovery key</summary>
						<div class="row">
							<input class="inp mono" bind:value={recoveryInput} placeholder="A1-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX-X" autocomplete="off" spellcheck="false" aria-label="Recovery key" />
							<button type="button" class="btn sm primary" onclick={unlock} disabled={unlocking || recoveryInput.trim().length < 20}>{unlocking ? 'Restoring…' : 'Restore'}</button>
						</div>
					</details>
				{/if}
			</div>
		{:else}
			<div class="notice">
				<h4>Set up this worker</h4>
				<p>Join another worker's network with its invite, or keep this worker's own settings.</p>
				{#if $isAdmin}
					<JoinNetworkPanel {status} onChanged={loadStatus} />
					<div class="row"><button type="button" class="btn sm" onclick={startFresh} disabled={startingFresh}>{startingFresh ? 'Setting up…' : 'Keep separate settings'}</button></div>
				{/if}
			</div>
		{/if}
	{:else if switchingToAccount}
		<div class="notice" role="status">
			<h4><Loader2 size={14} class="spin" /> Switching to your account's settings</h4>
			<p>Your account already syncs settings between {accountWorkers.length ? accountWorkers.map((d) => d.name || 'a worker').join(', ') : 'your other workers'}. This worker takes the account's settings; anything only this worker has is added to the account.</p>
			{#if status?.hub?.last_error}
				<p class="error">{status.hub.last_error}</p>
				{#if $isAdmin}<div class="row"><button type="button" class="btn sm" onclick={adopt} disabled={adopting}>{adopting ? 'Retrying…' : 'Retry'}</button></div>{/if}
			{/if}
		</div>
	{/if}
	{#if stale && status}<p class="stale" role="status"><AlertTriangle size={13} /> The worker did not answer the last status check; showing saved data.</p>{/if}
	{#if enrolled && status?.hub?.linked}
		<div class="notice" role="status">
			{#if status.hub.backup_enabled === false}
				<h4>Sync directly between workers</h4>
				<p>Your data stays on your workers. They catch up automatically when they can reach each other. If no other worker is online, this worker keeps your changes until one returns.</p>
				<p>Cloud backup is off. Restoring after losing every worker requires a separate backup.</p>
			{:else if status.hub.remote_durability === 'pending' || (status.hub.backlog ?? 0) > 0}
				<h4>Saved on this worker · cloud backup pending</h4>
				<p>{status.hub.backlog ?? 0} changes and {status.hub.files_pending_backup ?? 0} files are waiting for cloud backup. Keep this worker running until backup completes so offline devices can catch up.</p>
			{:else if status.hub.remote_durability === 'committed'}
				<h4>Cloud backup up to date</h4>
			{:else}
				<h4>Checking cloud backup</h4>
			{/if}
			{#if (status.hub.incoming_pending ?? 0) > 0}<p>{status.hub.incoming_pending} downloaded changes are waiting for missing updates from another worker.</p>{/if}
			{#if status.hub.last_error}<p class="error">{status.hub.last_error}</p>{/if}
			{#if $isAdmin && status.hub.backup_enabled !== undefined}
				<p>Optional cloud backup uploads an encrypted copy from this worker and uses shared storage and transfer allowances. Turning it off keeps existing cloud copies.</p>
				<button type="button" class="btn sm" disabled={savingBackup} onclick={toggleBackup}>{status.hub.backup_enabled ? 'Turn off cloud backup' : 'Enable cloud backup on this worker'}</button>
			{/if}
		</div>
	{/if}
	{#if revokedNotice}
		<div class="notice warn" role="alert">
			<p><strong>{revokedNotice}</strong> was removed. Its copies of your provider API keys are assumed compromised; rotate those keys with each provider.</p>
			<div class="row"><button type="button" class="btn sm" onclick={openProviders}>Open providers</button><button type="button" class="btn sm" onclick={() => (revokedNotice = null)}>Dismiss</button></div>
		</div>
	{/if}

	{#if workersError}<div class="load-error"><p role="alert">Workers: {workersError}</p><button type="button" class="btn sm" onclick={loadWorkers}>Retry workers</button></div>{/if}
	{#if clientsError}<div class="load-error"><p role="alert">Phones and browsers: {clientsError}</p><button type="button" class="btn sm" onclick={loadClients}>Retry phones and browsers</button></div>{/if}

	{#if !clientsLoaded && !clientsError}<p class="empty" role="status">Loading devices…</p>{/if}
	<ul class="list" aria-label="Devices">
		{#if status}
			<li class="row-item" data-device-key="self">
				<span class="icon" aria-hidden="true"><Server size={18} /></span>
				<div class="main">
					{#if editing === 'self'}
						<div class="editor">
							<input class="inp" aria-label="Device name" bind:value={editName} maxlength="100" disabled={saving !== null} onkeydown={(event) => { if (event.key === 'Enter') void saveEdit(); if (event.key === 'Escape' && !saving) cancelEdit(); }} />
							<button type="button" class="btn icon" aria-label="Save name" title="Save name" disabled={saving !== null || !editName.trim()} onclick={saveEdit}><Check size={15} /></button>
							<button type="button" class="btn icon" aria-label="Cancel rename" title="Cancel rename" disabled={saving !== null} onclick={cancelEdit}><X size={15} /></button>
						</div>
					{:else}
						<div class="name-line"><span class="name">{workerName}</span><span class="self">This worker</span></div>
					{/if}
					<div class="meta">
						{#if status.platform}<span>{status.platform}</span>{/if}
						{#if status.version}<span>v{status.version}</span>{/if}
						{#if enrolled && status.network}<span>{status.network.name || 'Your network'} · {status.network.member_count} {status.network.member_count === 1 ? 'worker' : 'workers'}</span>{:else}<span>Not in a network</span>{/if}
					</div>
				</div>
				{#if editing !== 'self' && $isAdmin && enrolled && self}
					<div class="actions"><button type="button" class="btn icon" aria-label="Rename {workerName}" title="Rename this worker" disabled={saving !== null} onclick={() => startEdit('self', self!.name)}><Pencil size={15} /></button></div>
				{/if}
			</li>
		{/if}
		{#each members as member (member.device_id)}
			{@const key = `member:${member.device_id}`}
			<li class="row-item" data-device-key={key}>
				<span class="icon" aria-hidden="true"><Server size={18} /></span>
				<div class="main">
					{#if editing === key}
						<div class="editor">
							<input class="inp" aria-label="Device name" bind:value={editName} maxlength="100" disabled={saving !== null} onkeydown={(event) => { if (event.key === 'Enter') void saveEdit(); if (event.key === 'Escape' && !saving) cancelEdit(); }} />
							<button type="button" class="btn icon" aria-label="Save name" title="Save name" disabled={saving !== null || !editName.trim()} onclick={saveEdit}><Check size={15} /></button>
							<button type="button" class="btn icon" aria-label="Cancel rename" title="Cancel rename" disabled={saving !== null} onclick={cancelEdit}><X size={15} /></button>
						</div>
					{:else}
						<div class="name-line"><span class="dot" class:online={member.online} title={member.online ? 'Online' : 'Offline'}></span><span class="name">{member.name}</span></div>
					{/if}
					<div class="meta">
						{#if member.platform}<span>{member.platform}</span>{/if}
						{#if member.version}<span>v{member.version}</span>{/if}
						<span>{member.online ? 'Online' : seen(member.last_seen)}</span>
					</div>
				</div>
				{#if editing !== key && confirmation?.key !== key && $isAdmin}
					<div class="actions">
						<button type="button" class="btn icon" aria-label="Rename {member.name}" title="Rename worker" disabled={saving !== null} onclick={() => startEdit(key, member.name)}><Pencil size={15} /></button>
						<button type="button" class="btn icon" aria-label="Revoke {member.name}" title="Revoke device access" disabled={saving !== null} onclick={() => { confirmation = { key, action: 'remove' }; }}><ShieldOff size={15} /></button>
					</div>
				{/if}
				{#if confirmation?.key === key}
					<div class="confirm">
						<p>Revoke access for <strong>{member.name}</strong>? {#if registrations.some((device) => device.localKey === key)}This also revokes its account access.{/if} It stops receiving your settings and sessions. Its copies of provider keys should be rotated afterwards.</p>
						<div class="row">
							<button type="button" class="btn sm danger" disabled={saving !== null} onclick={() => revokeAccess(key, member.name)}>Confirm revoke</button>
							<button type="button" class="btn sm" disabled={saving !== null} onclick={() => { confirmation = null; }}>Cancel</button>
						</div>
					</div>
				{/if}
			</li>
		{/each}
		{#each joining as device (device.key)}
			<li class="row-item joining" data-device-key={device.key}>
				<span class="icon" aria-hidden="true"><Loader2 size={18} class="spin" /></span>
				<div class="main">
					<div class="name-line"><span class="name">{device.name}</span></div>
					<div class="meta">{#if device.platform}<span>{device.platform}</span>{/if}<span>Joining {device.via === 'account' ? 'through your account' : 'with an invite'}…</span></div>
				</div>
			</li>
		{/each}
		{#each legacyInstalls as install (install.id)}
			{@const key = `legacy:${install.id}`}
			<li class="row-item" data-device-key={key}>
				<span class="icon" aria-hidden="true"><Server size={18} /></span>
				<div class="main">
					{#if editing === key}
						<div class="editor">
							<input class="inp" aria-label="Device name" bind:value={editName} maxlength="60" disabled={saving !== null} onkeydown={(event) => { if (event.key === 'Enter') void saveEdit(); if (event.key === 'Escape' && !saving) cancelEdit(); }} />
							<button type="button" class="btn icon" aria-label="Save name" title="Save name" disabled={saving !== null || !editName.trim()} onclick={saveEdit}><Check size={15} /></button>
							<button type="button" class="btn icon" aria-label="Cancel rename" title="Cancel rename" disabled={saving !== null} onclick={cancelEdit}><X size={15} /></button>
						</div>
					{:else}
						<div class="name-line"><span class="dot" class:online={install.online}></span><span class="name">{install.device_name}</span><span class="tag">Shared database</span></div>
					{/if}
					<div class="meta">
						{#if install.platform}<span>{install.platform}</span>{/if}
						{#if install.approved === false}<span class="warn-text">Not yet allowed to run tasks</span>{:else}<span>{install.online ? 'Online' : seen(install.last_seen)}</span>{/if}
					</div>
				</div>
				{#if editing !== key && confirmation?.key !== key && $isAdmin}
					<div class="actions">
						{#if install.approved === false}
							<button type="button" class="btn sm" aria-label="Allow {install.device_name}" disabled={saving !== null} onclick={() => approveLegacy(install, true)}><ShieldCheck size={15} /> Allow</button>
						{:else}
							<button type="button" class="btn icon" aria-label="Rename {install.device_name}" title="Rename device" disabled={saving !== null} onclick={() => startEdit(key, install.device_name)}><Pencil size={15} /></button>
							<button type="button" class="btn icon" aria-label="Revoke {install.device_name}" title="Revoke worker access" disabled={saving !== null} onclick={() => { confirmation = { key, action: 'revoke' }; }}><ShieldOff size={15} /></button>
						{/if}
						<button type="button" class="btn icon" aria-label="Remove {install.device_name}" title="Remove device" disabled={saving !== null} onclick={() => { confirmation = { key, action: 'remove' }; }}><Trash2 size={15} /></button>
					</div>
				{/if}
				{#if confirmation?.key === key}
					<div class="confirm">
						<p>{confirmation.action === 'remove' ? 'Remove this device from the list? It may reappear if it reconnects.' : 'Revoke access? This worker will no longer receive new tasks.'} {#if registrations.some((device) => device.localKey === key)}This also revokes its account access.{/if}</p>
						<div class="row">
							<button type="button" class="btn sm danger" disabled={saving !== null} onclick={() => revokeAccess(key, install.device_name, confirmation?.action)}>{confirmation.action === 'remove' ? 'Confirm remove' : 'Confirm revoke'}</button>
							<button type="button" class="btn sm" disabled={saving !== null} onclick={() => { confirmation = null; }}>Cancel</button>
						</div>
					</div>
				{/if}
			</li>
		{/each}
		{#each clients as client (client.id)}
			{@const key = `client:${client.id}`}
			<li class="row-item" data-device-key={key}>
				<span class="icon" aria-hidden="true">{#if clientKind(client) === 'Phone'}<Smartphone size={18} />{:else}<Globe size={18} />{/if}</span>
				<div class="main">
					<div class="name-line"><span class="name">{client.device_name}</span></div>
					<div class="meta">
						<span>{clientKind(client)}</span>
						{#if client.platform && !/^(browser|pwa|mobile)$/i.test(client.platform)}<span>{client.platform.replace(/-browser$/i, '')}</span>{/if}
						<span>{seen(client.last_seen)}</span>
					</div>
				</div>
				{#if confirmation?.key !== key && $isAdmin}
					<div class="actions"><button type="button" class="btn icon" aria-label="Revoke {client.device_name}" title="Revoke device access" disabled={saving !== null} onclick={() => { confirmation = { key, action: 'revoke' }; }}><ShieldOff size={15} /></button></div>
				{/if}
				{#if confirmation?.key === key}
					<div class="confirm">
						<p>Revoke access? {#if registrations.some((device) => device.localKey === key)}This also revokes its account access.{/if} This device will need to pair or sign in again.</p>
						<div class="row">
							<button type="button" class="btn sm danger" disabled={saving !== null} onclick={() => revokeAccess(key, client.device_name)}>Confirm revoke</button>
							<button type="button" class="btn sm" disabled={saving !== null} onclick={() => { confirmation = null; }}>Cancel</button>
						</div>
					</div>
				{/if}
			</li>
		{/each}
		{#each accountDevices as device (device.key)}
			<li class="row-item" data-device-key={device.key}>
				<span class="icon" aria-hidden="true">{#if device.kind === 'Worker'}<Server size={18} />{:else if device.kind === 'Phone'}<Smartphone size={18} />{:else if device.kind === 'Browser'}<Globe size={18} />{:else}<MonitorSmartphone size={18} />{/if}</span>
				<div class="main">
					<div class="name-line"><span class="name">{device.name}</span><span class="tag">On your account</span></div>
					<div class="meta"><span>{device.kind}</span>{#if device.platform}<span>{device.platform}</span>{/if}<span>Availability unknown</span></div>
				</div>
				{@render accountControl(device)}
			</li>
		{/each}
	</ul>
	{#if accountProfile && accountProfile.devices === undefined}<p class="empty">Update the account service to include all account phones and browsers.</p>{/if}

	{#if error}<p class="feedback error" role="alert">{error}</p>{/if}
	{#if success}<p class="feedback" role="status">{success}</p>{/if}

</section>

<style>
	.devices { border: 1px solid var(--border); border-radius: var(--radius); background: var(--card); color: var(--foreground); min-width: 0; }
	.head { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 14px; border-bottom: 1px solid var(--border); }
	.heading { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 12px; min-width: 0; }
	h3 { display: flex; align-items: center; gap: 8px; margin: 0; font-family: var(--font-display); font-size: 15px; font-weight: 700; letter-spacing: -.01em; }
	.count { color: var(--muted-foreground); font-size: 12px; font-variant-numeric: tabular-nums; white-space: nowrap; }
	.pill { padding: 2px 9px; border-radius: 999px; font-size: 11px; font-weight: 500; background: var(--muted); color: var(--muted-foreground); }
	.pill.s-ready { background: color-mix(in oklch, var(--success) 15%, transparent); color: var(--success); }
	.pill.s-syncing, .pill.s-waiting { background: color-mix(in oklch, var(--primary) 15%, transparent); color: var(--primary); }
	.pill.s-needs-setup { background: color-mix(in oklch, var(--warning, orange) 18%, transparent); color: var(--warning-foreground, var(--foreground)); }
	.pill.s-offline { background: color-mix(in oklch, var(--destructive) 12%, transparent); color: var(--destructive); }
	.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 40px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; background: transparent; color: var(--foreground); font: inherit; font-size: 12px; line-height: 1.4; cursor: pointer; }
	.btn:hover:not(:disabled) { background: var(--hover-overlay); }
	.btn:disabled { opacity: .5; cursor: default; }
	.btn:focus-visible, .inp:focus-visible, summary:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.btn.add { flex-shrink: 0; min-height: 36px; padding: 6px 10px; color: var(--primary); }
	.btn.sm { min-height: 34px; padding: 6px 10px; }
	.btn.primary { background: var(--primary); color: var(--primary-foreground); border-color: var(--primary); }
	.btn.icon { width: 40px; padding: 0; border-color: transparent; color: var(--muted-foreground); flex-shrink: 0; }
	.btn.danger, .error, .warn-text { color: var(--destructive); }
	.warn-text { color: var(--warning-foreground, var(--warning, var(--destructive))); }
	.notice { display: flex; flex-direction: column; gap: 8px; margin: 12px 18px 0; padding: 12px 14px; border: 1px solid var(--border); border-radius: 10px; background: color-mix(in oklch, var(--primary) 4%, var(--card)); }
	.notice.warn { border-color: color-mix(in oklch, var(--warning, orange) 50%, var(--border)); background: color-mix(in oklch, var(--warning, orange) 6%, var(--card)); }
	.notice.error-notice { border-color: color-mix(in oklch, var(--destructive) 40%, var(--border)); }
	.notice h4 { display: flex; align-items: center; gap: 6px; margin: 0; font-size: 13px; font-weight: 600; }
	.notice p, .confirm p, .load-error p { margin: 0; font-size: 12px; line-height: 1.5; color: var(--muted-foreground); overflow-wrap: anywhere; }
	.notice details summary { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted-foreground); cursor: pointer; min-height: 32px; }
	.row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
	.inp { box-sizing: border-box; min-width: 0; min-height: 40px; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--background); color: var(--foreground); font: inherit; font-size: 13px; }
	.inp.mono { flex: 1 1 220px; font-family: var(--font-mono); font-size: 12px; }
	.inp:disabled { opacity: .55; }
	.stale { display: flex; align-items: center; gap: 6px; margin: 8px 18px 0; font-size: 12px; color: var(--muted-foreground); }
	.list { list-style: none; margin: 0; padding: 0 14px; }
	.row-item { display: grid; grid-template-columns: 22px minmax(0, 1fr) auto; align-items: center; gap: 8px 10px; padding: 10px 0; }
	.row-item + .row-item { border-top: 1px solid var(--border); }
	.row-item.joining .icon { color: var(--primary); }
	.icon { display: flex; color: var(--muted-foreground); }
	.main { min-width: 0; }
	.name-line { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 8px; }
	.name { font-size: 13px; font-weight: 600; overflow-wrap: anywhere; }
	.self, .tag { font-size: 11px; color: var(--primary); white-space: nowrap; }
	.tag { color: var(--muted-foreground); }
	.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted-foreground); opacity: .5; flex-shrink: 0; }
	.dot.online { background: var(--success); opacity: 1; }
	.meta { display: flex; flex-wrap: wrap; gap: 2px 8px; margin-top: 3px; font-size: 11px; line-height: 1.5; color: var(--muted-foreground); }
	.meta span { overflow-wrap: anywhere; }
	.meta span + span::before { content: '·'; margin-right: 8px; }
	.actions { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }
	.editor { display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }
	.editor .inp { flex: 1 1 120px; width: 100%; }
	.confirm { grid-column: 2 / -1; display: flex; flex-direction: column; gap: 8px; }
	.empty { padding: 12px 0; font-size: 12px; line-height: 1.5; color: var(--muted-foreground); list-style: none; }
	p.empty { margin: 0; padding: 12px 18px; }
	.feedback { margin: 0; padding: 12px 18px; font-size: 12px; line-height: 1.5; color: var(--primary); }
	.feedback.error { color: var(--destructive); }
	.load-error { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 12px 18px; color: var(--destructive); }
	.load-error p { color: var(--destructive); }
	:global(.devices .spin) { animation: devices-spin 1s linear infinite; }
	@keyframes devices-spin { to { transform: rotate(360deg); } }
	@media (max-width: 560px) {
		.head { padding: 12px; }
		.notice, .stale { margin-left: 12px; margin-right: 12px; }
		.list, .feedback, .load-error { padding-left: 12px; padding-right: 12px; margin-left: 0; margin-right: 0; }
		.row-item { grid-template-columns: 20px minmax(0, 1fr); gap: 6px 8px; }
		.actions { grid-column: 2; justify-content: flex-end; }
		.btn, .inp { min-height: 44px; }
		.btn.icon { width: 44px; }
	}
</style>
