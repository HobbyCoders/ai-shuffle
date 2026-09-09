<script lang="ts">
	/**
	 * NetworkAdvanced — the parts of the worker network most people never
	 * touch: the recovery key, this worker's reachable address, sync health,
	 * conflicts, what syncs, and joining a different worker's network.
	 */
	import { onMount } from 'svelte';
	import { Activity, AlertTriangle, Check, Copy, KeyRound, Loader2, X } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { isAdmin } from '$lib/stores/auth.svelte';
	import {
		createRecoveryKey, dismissNetworkConflict, getNetworkPolicy, listNetworkConflicts,
		type CreatedRecoveryKey, type NetworkConflict, type NetworkPolicy, type NetworkStatus
	} from '$lib/api/network';
	import { contactAge, relativeTime } from '$lib/api/network-status';
	import { getAdvertiseStatus, setAdvertiseUrl, testAdvertiseReachability, type AdvertiseReachability, type AdvertiseStatus } from '$lib/api/installs';
	import JoinNetworkPanel from './JoinNetworkPanel.svelte';

	let { status, onChanged }: { status: NetworkStatus | null; onChanged?: () => void } = $props();

	let error = $state('');
	let copied = $state(false);
	let now = $state(Date.now());

	// Recovery key
	let recoveryBusy = $state(false);
	let confirmReplace = $state(false);
	let createdKey = $state<CreatedRecoveryKey | null>(null);
	let keySaved = $state(false);

	// Address
	let advertise = $state<AdvertiseStatus | null>(null);
	let advertiseInput = $state('');
	let advertiseError = $state('');
	let savingAdvertise = $state(false);
	let testingAdvertise = $state(false);
	let reachability = $state<AdvertiseReachability | null>(null);
	let addressNotice = $state('');

	// Conflicts, policy
	let conflicts = $state<NetworkConflict[]>([]);
	let dismissing = $state<number | null>(null);
	let policy = $state<NetworkPolicy | null>(null);
	let policyLoading = $state(false);
	let stopped = false;

	const enrolled = $derived(Boolean(status?.enrolled));
	const recordTotal = $derived(Object.values(status?.sync?.records ?? {}).reduce((a, b) => a + b, 0));
	const peers = $derived(status?.sync?.peers ?? []);

	function memberName(deviceId: string | null | undefined): string {
		if (!deviceId) return 'unknown device';
		return status?.members?.find((m) => m.device_id === deviceId)?.name ?? deviceId.slice(0, 12);
	}

	onMount(() => {
		void loadAdvertise();
		const tick = setInterval(() => { now = Date.now(); }, 30_000);
		return () => { stopped = true; clearInterval(tick); };
	});

	$effect(() => {
		const count = status?.sync?.conflicts ?? 0;
		if (enrolled && count > 0) void listNetworkConflicts().then((rows) => { if (!stopped) conflicts = rows; }).catch(() => {});
		else conflicts = [];
	});

	async function loadAdvertise() {
		try {
			const next = await getAdvertiseStatus();
			if (stopped) return;
			const clean = advertiseInput === (advertise?.override || '');
			advertise = next; advertiseError = '';
			if (clean) advertiseInput = next.override || '';
		} catch (e) {
			if (!stopped) advertiseError = getErrorMessage(e, 'Could not load the address');
		}
	}

	async function saveAdvertise() {
		if (!advertise || savingAdvertise || advertise.env_pinned) return;
		const url = advertiseInput.trim();
		if (url && !/^https?:\/\//.test(url)) { error = 'The address must start with http:// or https://'; return; }
		savingAdvertise = true; error = ''; addressNotice = '';
		try {
			advertise = await setAdvertiseUrl(url);
			advertiseInput = advertise.override || '';
			reachability = null;
			addressNotice = url ? 'Address saved' : 'Using the auto-detected address';
			onChanged?.();
		} catch (e) { error = getErrorMessage(e, 'Could not save the address'); }
		finally { savingAdvertise = false; }
	}

	async function testAdvertise() {
		testingAdvertise = true; error = '';
		try { reachability = await testAdvertiseReachability(); }
		catch (e) { error = getErrorMessage(e, 'Could not test the address'); }
		finally { testingAdvertise = false; }
	}

	async function makeRecoveryKey() {
		recoveryBusy = true; error = '';
		try {
			createdKey = await createRecoveryKey();
			keySaved = false; confirmReplace = false;
			onChanged?.();
		} catch (e) { error = getErrorMessage(e, 'Could not create the recovery key'); }
		finally { recoveryBusy = false; }
	}

	async function copyKey() {
		if (!createdKey) return;
		try { await navigator.clipboard.writeText(createdKey.recovery_key); copied = true; setTimeout(() => (copied = false), 1800); }
		catch { error = 'Clipboard access is unavailable. Select and copy the key manually.'; }
	}

	async function dismissConflict(id: number) {
		dismissing = id; error = '';
		try { await dismissNetworkConflict(id); conflicts = conflicts.filter((c) => c.id !== id); }
		catch (e) { error = getErrorMessage(e, 'Could not dismiss the conflict'); }
		finally { dismissing = null; }
	}

	async function loadPolicy(event: Event) {
		if (!(event.currentTarget as HTMLDetailsElement).open || policy || policyLoading) return;
		policyLoading = true;
		try { policy = await getNetworkPolicy(); }
		catch (e) { error = getErrorMessage(e, 'Could not load what syncs'); }
		finally { policyLoading = false; }
	}
</script>

<details class="advanced">
	<summary>Advanced</summary>
	<div class="body">
		<!-- Address -->
		<section class="block" aria-label="Worker address">
			<h5>This worker's address</h5>
			<p class="dim" id="device-address-help">Other workers and devices reach this worker here; invites and pairing links embed it. {#if advertise?.env_pinned}Set by CLUSTER_ADVERTISE_URL.{:else}Leave blank to detect it automatically.{/if}</p>
			<div class="row">
				<input id="device-address" class="inp" type="url" bind:value={advertiseInput} placeholder={advertise?.url || 'https://worker.example.com'} aria-label="This worker's address" aria-describedby="device-address-help" disabled={!advertise || advertise.env_pinned || savingAdvertise} autocomplete="off" />
				<button type="button" class="btn" disabled={testingAdvertise || savingAdvertise || !advertise?.url} onclick={testAdvertise}>{testingAdvertise ? 'Testing…' : 'Test address'}</button>
				<button type="button" class="btn" disabled={!advertise || advertise.env_pinned || savingAdvertise || advertiseInput.trim() === (advertise.override || '')} onclick={saveAdvertise}>{savingAdvertise ? 'Saving…' : 'Save address'}</button>
			</div>
			{#if reachability}<p class="dim" class:error={!reachability.ok} role="status">{reachability.detail}</p>{/if}
			{#if addressNotice}<p class="dim" role="status">{addressNotice}</p>{/if}
			{#if advertiseError}<div class="row"><p class="error" role="alert">{advertiseError}</p><button type="button" class="btn" onclick={loadAdvertise}>Retry address</button></div>{/if}
		</section>

		{#if enrolled && status}
			<!-- Recovery key -->
			<section class="block" aria-label="Recovery key">
				<h5><KeyRound size={14} /> Recovery key {#if status.recovery?.configured}<span class="b on">Saved</span>{:else}<span class="b warn">Not set</span>{/if}</h5>
				{#if createdKey}
					<div class="reveal" role="alert">
						<p class="warn-line"><AlertTriangle size={13} /> Save this key now. It will <strong>not</strong> be shown again.</p>
						<div class="row">
							<code class="key">{createdKey.recovery_key}</code>
							<button type="button" class="btn sm" onclick={copyKey}>{#if copied}<Check size={13} /> Copied{:else}<Copy size={13} /> Copy{/if}</button>
						</div>
						<p class="dim">With this key and your Shuffle account you can restore your settings onto a new worker when every other worker is off.</p>
						<label class="check"><input type="checkbox" bind:checked={keySaved} /><span>I saved this key somewhere safe.</span></label>
						<button type="button" class="btn sm primary" onclick={() => (createdKey = null)} disabled={!keySaved}>Done</button>
					</div>
				{:else}
					<p class="dim">
						{#if status.recovery?.configured}
							Created {relativeTime(status.recovery.created_at, now)}. A new key replaces the old one immediately.
						{:else}
							Without a recovery key your settings cannot be restored when every worker is lost. Create one and keep it with your other secrets.
						{/if}
					</p>
					<div class="row">
						{#if status.recovery?.configured}
							{#if confirmReplace}
								<button type="button" class="btn sm danger" onclick={makeRecoveryKey} disabled={recoveryBusy || !$isAdmin}>{recoveryBusy ? 'Replacing…' : 'Confirm new key'}</button>
								<button type="button" class="btn sm" onclick={() => (confirmReplace = false)} disabled={recoveryBusy}>Cancel</button>
							{:else}
								<button type="button" class="btn sm" onclick={() => (confirmReplace = true)} disabled={!$isAdmin}>Replace recovery key</button>
							{/if}
						{:else}
							<button type="button" class="btn sm primary" onclick={makeRecoveryKey} disabled={recoveryBusy || !$isAdmin}>{#if recoveryBusy}<Loader2 size={13} />{:else}<KeyRound size={13} />{/if} Create recovery key</button>
						{/if}
					</div>
				{/if}
			</section>

			<!-- Sync -->
			<section class="block" aria-label="Sync">
				<h5><Activity size={14} /> Sync</h5>
				<div class="stats">
					<div class="stat"><span class="n">{recordTotal}</span><span class="k">records</span></div>
					<div class="stat"><span class="n">{status.sync?.ops ?? 0}</span><span class="k">changes</span></div>
					<div class="stat"><span class="n">{status.sync?.holes?.length ?? 0}</span><span class="k">gaps</span></div>
					<div class="stat" class:bad={(status.sync?.conflicts ?? 0) > 0}><span class="n">{status.sync?.conflicts ?? 0}</span><span class="k">conflicts</span></div>
				</div>
				{#if peers.length > 0}
					<ul class="peers" aria-label="Peers">
						{#each peers as peer (peer.device_id)}
							<li><span class="dot" class:online={peer.online}></span><strong>{memberName(peer.device_id)}</strong><span class="dim">{peer.base_url ?? 'no address'} · last contact {contactAge(peer.seconds_since_contact)}</span>{#if peer.last_error}<span class="error">{peer.last_error}</span>{/if}</li>
						{/each}
					</ul>
				{/if}
				{#if status.hub?.linked}
					<p class="dim">Cloud: {status.hub.status ?? 'not registered'} · backlog {status.hub.backlog ?? 0} · pushed {relativeTime(status.hub.last_push_at, now)} · pulled {relativeTime(status.hub.last_pull_at, now)}</p>
					{#if status.hub.last_error}<p class="error">{status.hub.last_error}</p>{/if}
				{/if}
				{#if conflicts.length > 0}
					<p class="dim">Two workers changed the same record while apart. The newer change won; the losing copy is kept for a while.</p>
					<ul class="peers" aria-label="Conflicts">
						{#each conflicts as conflict (conflict.id)}
							<li><strong>{conflict.collection}</strong><code>{conflict.record_id}</code><span class="dim">won by {memberName(conflict.winning_device_id)} · lost by {memberName(conflict.losing_device_id)} · {relativeTime(conflict.created_at, now)}</span>
								{#if $isAdmin}<button type="button" class="btn icon" title="Dismiss" aria-label="Dismiss conflict" onclick={() => dismissConflict(conflict.id)} disabled={dismissing === conflict.id}><X size={13} /></button>{/if}</li>
						{/each}
					</ul>
				{/if}
				<details ontoggle={loadPolicy}>
					<summary>What syncs between workers</summary>
					{#if policyLoading}<p class="dim">Loading…</p>
					{:else if policy}
						<div class="policy">
							{#each policy.collections as collection (collection.collection)}
								<div class="prow"><strong>{collection.collection}</strong><span class="dim">{collection.mode}{collection.encrypted.length ? ` · encrypted: ${collection.encrypted.join(', ')}` : ''}{collection.notes ? ` · ${collection.notes}` : ''}</span></div>
							{/each}
						</div>
					{/if}
				</details>
			</section>

			<!-- Join another network -->
			{#if $isAdmin}
				<section class="block" aria-label="Join a different network">
					<h5>Join a different worker's network</h5>
					<p class="dim">Paste an invite from another worker to move this worker into that network instead of its current one.</p>
					<JoinNetworkPanel {status} replace {onChanged} />
				</section>
			{/if}
		{/if}
		{#if error}<p class="error" role="alert">{error}</p>{/if}
	</div>
</details>

<style>
	.advanced { border-top: 1px solid var(--border); color: var(--foreground); }
	summary { padding: 14px 18px; font-size: 12px; color: var(--muted-foreground); cursor: pointer; min-height: 44px; box-sizing: border-box; }
	summary:focus-visible, .btn:focus-visible, .inp:focus-visible { outline: 2px solid var(--ring); outline-offset: 2px; }
	.body { display: flex; flex-direction: column; gap: 16px; padding: 0 18px 16px; }
	.block { display: flex; flex-direction: column; gap: 8px; }
	h5 { display: flex; align-items: center; gap: 6px; margin: 0; font-size: 13px; font-weight: 600; }
	p { margin: 0; font-size: 12px; line-height: 1.5; overflow-wrap: anywhere; }
	.dim { color: var(--muted-foreground); }
	.error { color: var(--destructive); }
	.warn-line { display: flex; align-items: center; gap: 6px; color: var(--warning-foreground, var(--warning, var(--foreground))); }
	.row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
	.inp { box-sizing: border-box; flex: 1 1 220px; min-width: 0; min-height: 36px; padding: 6px 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--background); color: var(--foreground); font: inherit; font-size: 13px; }
	.inp:disabled { opacity: .55; }
	.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; min-height: 36px; padding: 6px 12px; border: 1px solid var(--border); border-radius: 8px; background: transparent; color: var(--foreground); font: inherit; font-size: 12px; cursor: pointer; }
	.btn:hover:not(:disabled) { background: var(--hover-overlay); }
	.btn:disabled { opacity: .55; cursor: default; }
	.btn.primary { background: var(--primary); color: var(--primary-foreground); border-color: var(--primary); }
	.btn.danger { color: var(--destructive); }
	.btn.sm { min-height: 32px; padding: 4px 10px; }
	.btn.icon { width: 32px; min-height: 32px; padding: 0; border-color: transparent; color: var(--muted-foreground); }
	.b { padding: 1px 8px; border-radius: 999px; font-size: 11px; font-weight: 500; }
	.b.on { background: color-mix(in oklch, var(--success) 15%, transparent); color: var(--success); }
	.b.warn { background: color-mix(in oklch, var(--warning, orange) 15%, transparent); color: var(--warning-foreground, var(--warning, var(--foreground))); }
	.reveal { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; padding: 12px; border: 1px solid var(--border); border-radius: 10px; }
	.key { font-family: var(--font-mono); font-size: 14px; letter-spacing: .04em; user-select: all; overflow-wrap: anywhere; }
	.check { display: flex; align-items: flex-start; gap: 8px; font-size: 12px; cursor: pointer; }
	.stats { display: flex; flex-wrap: wrap; gap: 14px; }
	.stat { display: flex; align-items: baseline; gap: 4px; }
	.stat .n { font-size: 16px; font-weight: 600; font-variant-numeric: tabular-nums; }
	.stat .k { font-size: 11px; color: var(--muted-foreground); }
	.stat.bad .n { color: var(--destructive); }
	.peers { display: flex; flex-direction: column; gap: 6px; margin: 0; padding: 0; list-style: none; }
	.peers li { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; font-size: 12px; }
	.peers code { font-family: var(--font-mono); font-size: 11px; }
	.dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted-foreground); opacity: .5; }
	.dot.online { background: var(--success); opacity: 1; }
	details details summary { padding: 8px 0; min-height: 0; }
	.policy { display: flex; flex-direction: column; gap: 4px; }
	.prow { display: flex; flex-wrap: wrap; gap: 4px 8px; font-size: 12px; }
	@media (max-width: 560px) { summary { padding: 12px; } .body { padding: 0 12px 12px; } .btn, .inp { min-height: 44px; } }
</style>
