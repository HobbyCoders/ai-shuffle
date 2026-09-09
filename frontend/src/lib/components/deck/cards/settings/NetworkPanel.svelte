<script lang="ts">
	/**
	 * NetworkPanel — the worker network: identity, members, approvals, invites,
	 * joining, the recovery key and sync health.
	 *
	 * Every worker owns a device identity and belongs to exactly one network.
	 * Status is polled every 5s while the panel is visible (3s while a join is
	 * pending) and refreshed on the `network` resource invalidation. A failed
	 * poll keeps the last payload on screen and flips the state pill to Offline.
	 */
	import { onDestroy, onMount } from 'svelte';
	import {
		Activity, AlertTriangle, Check, Copy, KeyRound, Link2, Loader2, LogIn, Network, Pencil, Plus,
		QrCode, RefreshCw, ShieldAlert, ShieldCheck, Trash2, Users, X
	} from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import { isAdmin } from '$lib/stores/auth.svelte';
	import { watchResources } from '$lib/services/watch-resources';
	import { listPairingEndpoints, revokePairingGrant, type PairingEndpoint } from '$lib/api/pairing';
	import {
		approveHubDevice, approveJoin, cancelEnrollment, createNetwork, createNetworkJoinGrant, createRecoveryKey,
		dismissNetworkConflict, getNetworkPolicy, getNetworkStatus, listNetworkConflicts, rejectHubDevice, rejectJoin,
		renameNetworkMember, revokeNetworkMember, startEnrollment,
		type CreatedRecoveryKey, type EnrollStarted, type NetworkConflict, type NetworkJoinGrant, type NetworkPolicy,
		type NetworkStatus
	} from '$lib/api/network';
	import {
		NETWORK_STATE_HINTS, NETWORK_STATE_LABELS, contactAge, deriveNetworkState, fingerprintFromPublicKey,
		formatCountdown, isCompleteFingerprint, parseJoinLink, pendingApprovalCount, relativeTime, secondsUntil
	} from '$lib/api/network-status';

	const STATUS_INTERVAL_MS = 5000;
	const ENROLL_INTERVAL_MS = 3000;

	// ── Status ──────────────────────────────────────────────────────────────
	let status = $state<NetworkStatus | null>(null);
	let stale = $state(false);
	let loading = $state(true);
	let error = $state('');
	let success = $state('');
	let conflicts = $state<NetworkConflict[]>([]);
	let now = $state(Date.now());
	let copied = $state<string | null>(null);

	const uiState = $derived(deriveNetworkState(status, stale));
	const self = $derived(status?.members?.find((m) => m.is_self) ?? null);
	const fingerprint = $derived(status?.device.fingerprint ?? '');
	const activeMembers = $derived((status?.members ?? []).filter((m) => m.status === 'active'));
	const revokedCount = $derived((status?.members ?? []).filter((m) => m.status !== 'active').length);
	const pendingJoins = $derived(status?.pending_joins ?? []);
	const hubPending = $derived(status?.hub?.pending_devices ?? []);
	const approvalCount = $derived(pendingApprovalCount(status));
	const enrollment = $derived(status && !status.enrolled ? status.enrollment ?? null : null);
	const enrollmentPending = $derived(enrollment?.status === 'pending');

	function memberName(deviceId: string | null | undefined): string {
		if (!deviceId) return 'unknown device';
		const member = status?.members?.find((m) => m.device_id === deviceId);
		return member?.name ?? deviceId.slice(0, 12);
	}

	// ── Polling ─────────────────────────────────────────────────────────────
	let pollTimer: ReturnType<typeof setTimeout> | null = null;
	let tickTimer: ReturnType<typeof setInterval> | null = null;
	let stopped = false;

	function schedulePoll() {
		if (stopped) return;
		if (pollTimer) clearTimeout(pollTimer);
		pollTimer = setTimeout(async () => {
			if (typeof document === 'undefined' || document.visibilityState === 'visible') await load(true);
			schedulePoll();
		}, enrollmentPending ? ENROLL_INTERVAL_MS : STATUS_INTERVAL_MS);
	}

	onMount(() => {
		void load();
		void loadEndpoints();
		schedulePoll();
		tickTimer = setInterval(() => {
			now = Date.now();
			if (invite && inviteSecondsLeft <= 0) invite = null;
		}, 1000);
	});

	onDestroy(() => {
		stopped = true;
		if (pollTimer) clearTimeout(pollTimer);
		if (tickTimer) clearInterval(tickTimer);
	});

	watchResources(['network', 'settings'], () => load(true));

	async function load(quiet = false) {
		if (!quiet) loading = true;
		try {
			const next = await getNetworkStatus();
			const wasPending = status?.enrollment?.status === 'pending';
			status = next;
			stale = false;
			if (next.enrolled) {
				conflicts = (next.sync?.conflicts ?? 0) > 0 ? await listNetworkConflicts().catch(() => conflicts) : [];
				void computeHubFingerprints(next.hub?.pending_devices ?? []);
				if (wasPending) {
					joinStarted = null;
					flash(`Joined ${next.network?.name ?? 'the network'}`);
				}
			} else {
				conflicts = [];
			}
			if (!editingName) nameInput = next.members?.find((m) => m.is_self)?.name ?? '';
		} catch (e: unknown) {
			stale = true;
			if (!quiet) error = getErrorMessage(e, 'Failed to load network status');
		} finally {
			loading = false;
		}
	}

	function flash(msg: string) {
		success = msg;
		setTimeout(() => { if (success === msg) success = ''; }, 3500);
	}

	async function copy(value: string, key: string) {
		try {
			await navigator.clipboard.writeText(value);
			copied = key;
			setTimeout(() => { if (copied === key) copied = null; }, 1800);
		} catch {
			error = 'Clipboard access is unavailable. Select and copy the value manually.';
		}
	}

	// ── This worker: rename ─────────────────────────────────────────────────
	let editingName = $state(false);
	let nameInput = $state('');
	let savingName = $state(false);

	async function saveName() {
		const name = nameInput.trim();
		if (!name || !self) return;
		savingName = true;
		error = '';
		try {
			await renameNetworkMember(self.device_id, name);
			editingName = false;
			flash('Worker renamed');
			await load(true);
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to rename this worker');
		} finally {
			savingName = false;
		}
	}

	// ── Create a network (fresh worker) ─────────────────────────────────────
	let networkName = $state('');
	let creatingNetwork = $state(false);

	async function create() {
		creatingNetwork = true;
		error = '';
		try {
			await createNetwork({ name: networkName });
			networkName = '';
			flash('Network created — create a recovery key next');
			await load(true);
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to create the network');
		} finally {
			creatingNetwork = false;
		}
	}

	// ── Members: remove ─────────────────────────────────────────────────────
	let confirmRemoveId = $state<string | null>(null);
	let removingId = $state<string | null>(null);
	let revokedNotice = $state<string | null>(null);

	async function remove(deviceId: string) {
		removingId = deviceId;
		error = '';
		const name = memberName(deviceId);
		try {
			const result = await revokeNetworkMember(deviceId);
			confirmRemoveId = null;
			revokedNotice = result.rotate_provider_keys_recommended ? name : null;
			flash(`${name} removed — network key rotated`);
			await load(true);
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to remove the device');
		} finally {
			removingId = null;
		}
	}

	function openProviders() {
		const target = document.getElementById('providers-section');
		if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
	}

	// ── Pending approvals ───────────────────────────────────────────────────
	let confirmed = $state<Record<string, boolean>>({});
	let hubFingerprints = $state<Record<string, string>>({});
	let approvalBusy = $state<string | null>(null);

	async function computeHubFingerprints(devices: { device_id: string; signing_public: string }[]) {
		for (const device of devices) {
			if (hubFingerprints[device.device_id]) continue;
			try {
				hubFingerprints[device.device_id] = await fingerprintFromPublicKey(device.signing_public);
			} catch {
				hubFingerprints[device.device_id] = 'unavailable';
			}
		}
	}

	async function decideJoin(joinId: string, approve: boolean) {
		approvalBusy = joinId;
		error = '';
		try {
			if (approve) await approveJoin(joinId); else await rejectJoin(joinId);
			delete confirmed[joinId];
			flash(approve ? 'Worker approved — it will sync shortly' : 'Join request rejected');
			await load(true);
		} catch (e: unknown) {
			error = getErrorMessage(e, approve ? 'Failed to approve the join' : 'Failed to reject the join');
		} finally {
			approvalBusy = null;
		}
	}

	async function decideHubDevice(deviceId: string, approve: boolean) {
		approvalBusy = deviceId;
		error = '';
		try {
			if (approve) await approveHubDevice(deviceId); else await rejectHubDevice(deviceId);
			delete confirmed[deviceId];
			flash(approve ? 'Device approved through your account' : 'Device rejected');
			await load(true);
		} catch (e: unknown) {
			error = getErrorMessage(e, approve ? 'Failed to approve the device' : 'Failed to reject the device');
		} finally {
			approvalBusy = null;
		}
	}

	// ── Invite another worker ───────────────────────────────────────────────
	let endpoints = $state<PairingEndpoint[]>([]);
	let selectedEndpoint = $state('');
	let invite = $state<NetworkJoinGrant | null>(null);
	let creatingInvite = $state(false);

	const inviteSecondsLeft = $derived(invite ? secondsUntil(invite.grant.expires_at, now) : 0);
	const inviteEndpoint = $derived(invite?.join?.endpoint ?? invite?.grant.endpoint_url ?? '');
	const inviteFingerprint = $derived(invite?.join?.fingerprint ?? fingerprint);

	async function loadEndpoints() {
		try {
			endpoints = await listPairingEndpoints();
			const usable = endpoints.filter((e) => !e.loopback);
			selectedEndpoint = usable.find((e) => e.qr_eligible)?.url ?? usable[0]?.url ?? endpoints[0]?.url ?? '';
		} catch {
			endpoints = [];
		}
	}

	async function createInvite() {
		if (!selectedEndpoint) return;
		creatingInvite = true;
		error = '';
		try {
			invite = await createNetworkJoinGrant(selectedEndpoint);
			now = Date.now();
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to create a join code');
		} finally {
			creatingInvite = false;
		}
	}

	async function cancelInvite() {
		if (!invite) return;
		const grant = invite;
		invite = null;
		try {
			await revokePairingGrant(grant.grant.id);
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to cancel the join code');
		}
	}

	// ── Join from another worker (this worker is fresh) ─────────────────────
	let joinInput = $state('');
	let joinEndpoint = $state('');
	let joinCode = $state('');
	let joinFingerprint = $state('');
	let joinDeviceName = $state('');
	let joinCompared = $state(false);
	let joinBusy = $state(false);
	let joinStarted = $state<EnrollStarted | null>(null);

	const parsedLink = $derived(parseJoinLink(joinInput));
	const joinReady = $derived.by(() => {
		if (parsedLink) return joinCompared && isCompleteFingerprint(parsedLink.fingerprint);
		return Boolean(joinEndpoint.trim() && joinCode.trim()) && isCompleteFingerprint(joinFingerprint) && joinCompared;
	});

	async function startJoin() {
		joinBusy = true;
		error = '';
		try {
			const deviceName = joinDeviceName.trim() || undefined;
			joinStarted = parsedLink
				? await startEnrollment({ link: joinInput, device_name: deviceName })
				: await startEnrollment({ endpoint_url: joinEndpoint, code: joinCode, inviter_fingerprint: joinFingerprint, device_name: deviceName });
			joinInput = ''; joinEndpoint = ''; joinCode = ''; joinFingerprint = ''; joinCompared = false;
			await load(true);
			schedulePoll();
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to start joining');
		} finally {
			joinBusy = false;
		}
	}

	async function cancelJoin() {
		joinBusy = true;
		error = '';
		try {
			await cancelEnrollment();
			joinStarted = null;
			await load(true);
			schedulePoll();
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to cancel the join');
		} finally {
			joinBusy = false;
		}
	}

	// ── Recovery key ────────────────────────────────────────────────────────
	let recoveryBusy = $state(false);
	let confirmRotate = $state(false);
	let createdKey = $state<CreatedRecoveryKey | null>(null);
	let keySaved = $state(false);

	async function makeRecoveryKey() {
		recoveryBusy = true;
		error = '';
		try {
			createdKey = await createRecoveryKey();
			keySaved = false;
			confirmRotate = false;
			await load(true);
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to create the recovery key');
		} finally {
			recoveryBusy = false;
		}
	}

	// ── Conflicts ───────────────────────────────────────────────────────────
	let dismissingConflict = $state<number | null>(null);

	async function dismissConflict(id: number) {
		dismissingConflict = id;
		error = '';
		try {
			await dismissNetworkConflict(id);
			conflicts = conflicts.filter((c) => c.id !== id);
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to dismiss the conflict');
		} finally {
			dismissingConflict = null;
		}
	}

	// ── Policy ("what syncs") ───────────────────────────────────────────────
	let policy = $state<NetworkPolicy | null>(null);
	let policyLoading = $state(false);

	async function loadPolicy(event: Event) {
		if (!(event.currentTarget as HTMLDetailsElement).open || policy || policyLoading) return;
		policyLoading = true;
		try {
			policy = await getNetworkPolicy();
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to load the sync policy');
		} finally {
			policyLoading = false;
		}
	}

	const recordTotal = $derived(Object.values(status?.sync?.records ?? {}).reduce((a, b) => a + b, 0));
</script>

<section class="net-panel" aria-label="Worker network">
	<!-- ── (a) This worker ──────────────────────────────────────────────── -->
	<div class="net-head">
		<div class="net-title">
			<h4><Network size={15} /> Worker network</h4>
			<p>Workers in a network sync settings, preferences, profiles and provider keys. Each session runs on one owner worker; the others mirror it.</p>
		</div>
		<span class="pill s-{uiState}" title={NETWORK_STATE_HINTS[uiState]}>{NETWORK_STATE_LABELS[uiState]}</span>
	</div>

	{#if loading && !status}
		<p class="muted"><Loader2 size={13} class="spin" /> Loading network…</p>
	{:else if status}
		<div class="self-grid">
			<div class="kv">
				<span class="k">This worker</span>
				{#if editingName}
					<span class="v row">
						<input class="inp name-in" bind:value={nameInput} maxlength="100" placeholder="Worker name"
							onkeydown={(e) => { if (e.key === 'Enter') saveName(); if (e.key === 'Escape') editingName = false; }} />
						<button type="button" class="icon" title="Save name" onclick={saveName} disabled={savingName || !nameInput.trim()}><Check size={14} /></button>
						<button type="button" class="icon" title="Cancel" onclick={() => (editingName = false)} disabled={savingName}><X size={14} /></button>
					</span>
				{:else}
					<span class="v row">
						<strong>{self?.name ?? status.platform ?? 'This worker'}</strong>
						{#if status.platform}<span class="dim">{status.platform}</span>{/if}
						{#if status.version}<span class="dim">v{status.version}</span>{/if}
						{#if status.enrolled && $isAdmin && self}
							<button type="button" class="icon" title="Rename this worker" onclick={() => { nameInput = self?.name ?? ''; editingName = true; }}><Pencil size={13} /></button>
						{/if}
					</span>
				{/if}
			</div>
			<div class="kv">
				<span class="k">Fingerprint</span>
				<span class="v row">
					<code class="fp">{fingerprint || '—'}</code>
					{#if fingerprint}
						<button type="button" class="icon" title="Copy fingerprint" onclick={() => copy(fingerprint, 'fp')}>
							{#if copied === 'fp'}<Check size={14} />{:else}<Copy size={14} />{/if}
						</button>
					{/if}
				</span>
			</div>
			<div class="kv">
				<span class="k">Network</span>
				<span class="v row">
					{#if status.enrolled && status.network}
						<strong>{status.network.name}</strong>
						<span class="dim">{status.network.member_count} member{status.network.member_count === 1 ? '' : 's'} · key v{status.network.key_version} · generation {status.network.generation}</span>
					{:else}
						<span class="dim">Not enrolled</span>
					{/if}
				</span>
			</div>
		</div>
		{#if stale}
			<p class="warn-line" role="status"><AlertTriangle size={13} /> The worker did not answer the last status check — showing saved data.</p>
		{/if}

		{#if !status.enrolled}
			<!-- ── (e) Join from another worker / start fresh ─────────────── -->
			<div class="block">
				{#if enrollmentPending && enrollment}
					<div class="waiting" role="status">
						<h5><Loader2 size={14} class="spin" /> Waiting for approval on {joinStarted?.inviter.name || enrollment.endpoint_url || 'the inviting worker'}</h5>
						<p>On that worker, open Settings → Devices → Pending approvals and compare this fingerprint before approving:</p>
						<div class="row">
							<code class="fp big">{enrollment.fingerprint || joinStarted?.your_fingerprint || fingerprint}</code>
							<button type="button" class="icon" title="Copy fingerprint" onclick={() => copy(enrollment.fingerprint || fingerprint, 'own')}>
								{#if copied === 'own'}<Check size={14} />{:else}<Copy size={14} />{/if}
							</button>
						</div>
						{#if joinStarted?.inviter.network_name}<p class="dim">Network: {joinStarted.inviter.network_name}</p>{/if}
						<p class="dim">Inviter fingerprint you verified: <code class="fp">{enrollment.inviter_fingerprint}</code></p>
						<button type="button" class="ed-btn sm" onclick={cancelJoin} disabled={joinBusy || !$isAdmin}><X size={13} /> Cancel join</button>
					</div>
				{:else}
					{#if enrollment && enrollment.status !== 'complete'}
						<div class="fail" role="alert">
							<ShieldAlert size={14} />
							<span>The last join {enrollment.status}{enrollment.error ? `: ${enrollment.error}` : ''}.</span>
							<button type="button" class="ed-btn sm" onclick={cancelJoin} disabled={joinBusy || !$isAdmin}>Dismiss</button>
						</div>
					{/if}
					<h5><LogIn size={14} /> Join from another worker</h5>
					<p class="dim">On the other worker, open Settings → Devices → Invite another worker. Scan or paste its link, or type the code and address. You must also read the inviter's fingerprint from that screen — it proves you are talking to the right worker.</p>
					<label class="field">
						<span>Join link</span>
						<input class="inp" bind:value={joinInput} placeholder="shuffle://network/join?endpoint=…&code=…&fp=…" autocomplete="off" spellcheck="false" />
					</label>
					{#if parsedLink}
						<div class="parsed">
							<div><span class="k">Address</span><code>{parsedLink.endpoint || '—'}</code></div>
							<div><span class="k">Code</span><code>{parsedLink.code || '—'}</code></div>
							<div><span class="k">Inviter fingerprint</span><code class="fp">{parsedLink.fingerprint || '—'}</code></div>
						</div>
					{:else}
						<div class="manual">
							<label class="field"><span>Worker address</span><input class="inp" bind:value={joinEndpoint} placeholder="https://10.0.0.5:8000" autocomplete="off" spellcheck="false" /></label>
							<label class="field"><span>Join code</span><input class="inp" bind:value={joinCode} placeholder="ABCD-EFGH-JKLM" autocomplete="off" spellcheck="false" /></label>
							<label class="field wide"><span>Inviter fingerprint (read it on the other screen)</span><input class="inp mono" bind:value={joinFingerprint} placeholder="xxxx-xxxx-xxxx-xxxx-xxxx-xxxx-xxxx-xxxx" autocomplete="off" spellcheck="false" /></label>
						</div>
					{/if}
					<label class="field"><span>Name for this worker (optional)</span><input class="inp" bind:value={joinDeviceName} maxlength="100" placeholder={status.platform ? `${status.platform} worker` : 'Worker name'} /></label>
					<label class="check">
						<input type="checkbox" bind:checked={joinCompared} />
						<span>I compared the inviter's fingerprint with the one shown on the other worker.</span>
					</label>
					<div class="actions">
						<button type="button" class="ed-btn primary" onclick={startJoin} disabled={joinBusy || !joinReady || !$isAdmin}>
							{#if joinBusy}<Loader2 size={13} class="spin" />{:else}<LogIn size={13} />{/if} Join network
						</button>
					</div>

					<div class="divider"></div>
					<h5><Plus size={14} /> Or start a new network here</h5>
					<p class="dim">This worker becomes the first member. You can invite other workers afterwards.</p>
					<div class="row">
						<input class="inp" bind:value={networkName} maxlength="100" placeholder="Network name (optional)" />
						<button type="button" class="ed-btn" onclick={create} disabled={creatingNetwork || !$isAdmin}>
							{#if creatingNetwork}<Loader2 size={13} class="spin" />{:else}<Plus size={13} />{/if} Start network
						</button>
					</div>
				{/if}
			</div>
		{:else}
			<!-- ── (b) Members ───────────────────────────────────────────── -->
			<div class="block">
				<h5><Users size={14} /> Members <span class="count">{activeMembers.filter((m) => m.online).length}/{activeMembers.length} online</span></h5>
				{#if revokedNotice}
					<div class="fail warn" role="alert">
						<ShieldAlert size={14} />
						<span><strong>{revokedNotice}</strong> was removed and the network key was rotated. Its copies of your provider API keys are assumed compromised — rotate those keys with each provider now.</span>
						<button type="button" class="ed-btn sm" onclick={openProviders}>Open providers</button>
						<button type="button" class="icon" title="Dismiss" onclick={() => (revokedNotice = null)}><X size={13} /></button>
					</div>
				{/if}
				<div class="list">
					{#each activeMembers as member (member.device_id)}
						<div class="mrow" class:self={member.is_self}>
							<span class="dot" class:online={member.online} title={member.online ? 'Online' : 'Offline'}></span>
							<div class="mmain">
								<div class="l1">
									<strong>{member.name}</strong>
									{#if member.is_self}<span class="b b-on">This worker</span>{/if}
									{#if member.kind !== 'worker'}<span class="b b-muted">{member.kind}</span>{/if}
								</div>
								<div class="l2">
									{#if member.platform}<span>{member.platform}</span><span class="sep">·</span>{/if}
									{#if member.version}<span>v{member.version}</span><span class="sep">·</span>{/if}
									<span>{member.online ? 'online' : `seen ${relativeTime(member.last_seen, now)}`}</span>
								</div>
								<code class="fp small">{member.fingerprint ?? '—'}</code>
							</div>
							{#if $isAdmin && !member.is_self}
								{#if confirmRemoveId === member.device_id}
									<div class="confirm">
										<p><AlertTriangle size={13} /> Removing <strong>{member.name}</strong> rotates the network key. Its copies of your provider API keys are assumed compromised — plan to rotate them.</p>
										<div class="actions">
											<button type="button" class="ed-btn sm danger" onclick={() => remove(member.device_id)} disabled={removingId === member.device_id}>
												{removingId === member.device_id ? 'Removing…' : 'Remove and rotate key'}
											</button>
											<button type="button" class="ed-btn sm" onclick={() => (confirmRemoveId = null)} disabled={removingId === member.device_id}>Cancel</button>
										</div>
									</div>
								{:else}
									<button type="button" class="icon" title="Remove from network" onclick={() => (confirmRemoveId = member.device_id)}><Trash2 size={13} /></button>
								{/if}
							{/if}
						</div>
					{/each}
				</div>
				{#if revokedCount > 0}<p class="dim small">{revokedCount} revoked device{revokedCount === 1 ? '' : 's'} no longer receive network data.</p>{/if}
			</div>

			<!-- ── (c) Pending approvals ─────────────────────────────────── -->
			{#if approvalCount > 0}
				<div class="block attention">
					<h5><ShieldCheck size={14} /> Pending approvals <span class="count">{approvalCount}</span></h5>
					<p class="dim">Compare each fingerprint with the one displayed on the joining device. Approve only when they match exactly.</p>
					{#each pendingJoins as join (join.id)}
						<div class="approval">
							<div class="amain">
								<strong>{join.name}</strong>
								<span class="dim">{join.platform ?? 'unknown platform'} · asked {relativeTime(join.created_at, now)}{join.expires_at ? ` · expires in ${formatCountdown(secondsUntil(join.expires_at, now))}` : ''}</span>
								<code class="fp big">{join.fingerprint ?? 'fingerprint unavailable'}</code>
								<label class="check">
									<input type="checkbox" bind:checked={confirmed[join.id]} disabled={!join.fingerprint} />
									<span>I compared the fingerprints and they match.</span>
								</label>
							</div>
							<div class="actions">
								<button type="button" class="ed-btn sm primary" onclick={() => decideJoin(join.id, true)} disabled={!confirmed[join.id] || approvalBusy === join.id || !$isAdmin}><ShieldCheck size={13} /> Approve</button>
								<button type="button" class="ed-btn sm" onclick={() => decideJoin(join.id, false)} disabled={approvalBusy === join.id || !$isAdmin}>Reject</button>
							</div>
						</div>
					{/each}
					{#each hubPending as device (device.device_id)}
						<div class="approval">
							<div class="amain">
								<strong>{device.name || device.device_id}</strong> <span class="b b-muted">via account</span>
								<span class="dim">{device.platform ?? 'unknown platform'}{device.created_at ? ` · registered ${relativeTime(device.created_at, now)}` : ''}</span>
								<code class="fp big">{hubFingerprints[device.device_id] ?? 'computing…'}</code>
								<label class="check">
									<input type="checkbox" bind:checked={confirmed[device.device_id]} disabled={!hubFingerprints[device.device_id] || hubFingerprints[device.device_id] === 'unavailable'} />
									<span>I compared the fingerprints and they match.</span>
								</label>
							</div>
							<div class="actions">
								<button type="button" class="ed-btn sm primary" onclick={() => decideHubDevice(device.device_id, true)} disabled={!confirmed[device.device_id] || approvalBusy === device.device_id || !$isAdmin}><ShieldCheck size={13} /> Approve</button>
								<button type="button" class="ed-btn sm" onclick={() => decideHubDevice(device.device_id, false)} disabled={approvalBusy === device.device_id || !$isAdmin}>Reject</button>
							</div>
						</div>
					{/each}
				</div>
			{/if}

			<!-- ── (d) Invite another worker ────────────────────────────── -->
			<div class="block">
				<h5><Link2 size={14} /> Invite another worker</h5>
				{#if invite}
					<div class="grant">
						<div class="grant-main">
							<div class="kv"><span class="k">Join code</span><span class="v row"><code class="code">{invite.display_code}</code>
								<button type="button" class="icon" title="Copy code" onclick={() => copy(invite!.display_code, 'code')}>{#if copied === 'code'}<Check size={14} />{:else}<Copy size={14} />{/if}</button></span></div>
							<div class="kv"><span class="k">Address</span><span class="v"><code>{inviteEndpoint || '—'}</code></span></div>
							<div class="kv"><span class="k">This worker's fingerprint</span><span class="v row"><code class="fp">{inviteFingerprint}</code>
								<button type="button" class="icon" title="Copy fingerprint" onclick={() => copy(inviteFingerprint, 'ifp')}>{#if copied === 'ifp'}<Check size={14} />{:else}<Copy size={14} />{/if}</button></span></div>
							<p class="dim">Read this fingerprint to the person at the other worker — they must type it before the join starts. Expires in <strong>{formatCountdown(inviteSecondsLeft)}</strong>.</p>
							<div class="actions">
								{#if invite.join?.url}
									<button type="button" class="ed-btn sm" onclick={() => copy(invite!.join!.url, 'link')}>{#if copied === 'link'}<Check size={13} /> Copied{:else}<Copy size={13} /> Copy join link{/if}</button>
								{/if}
								<button type="button" class="ed-btn sm" onclick={cancelInvite}><X size={13} /> Cancel code</button>
							</div>
						</div>
						{#if invite.qr_svg}
							<div class="qr" aria-label="Join QR code">{@html invite.qr_svg}</div>
						{/if}
					</div>
				{:else}
					<p class="dim">Creates a one-time code (valid for five minutes) bound to one of this worker's addresses. The joining worker must be able to reach that address.</p>
					<div class="row">
						<select class="inp" bind:value={selectedEndpoint} aria-label="Address the joining worker will use">
							{#each endpoints as endpoint (endpoint.url)}
								<option value={endpoint.url}>{endpoint.label} · {endpoint.url}{endpoint.loopback ? ' (only reachable from this machine)' : ''}</option>
							{/each}
							{#if endpoints.length === 0}<option value="">No reachable address — set one below</option>{/if}
						</select>
						<button type="button" class="ed-btn primary" onclick={createInvite} disabled={creatingInvite || !selectedEndpoint || !$isAdmin}>
							{#if creatingInvite}<Loader2 size={13} class="spin" />{:else}<QrCode size={13} />{/if} Create join code
						</button>
					</div>
				{/if}
			</div>

			<!-- ── (f) Recovery key ─────────────────────────────────────── -->
			<div class="block">
				<h5><KeyRound size={14} /> Recovery key
					{#if status.recovery?.configured}<span class="b b-on">Configured</span>{:else}<span class="b b-warn">Not set</span>{/if}
				</h5>
				{#if createdKey}
					<div class="reveal" role="alert">
						<p class="warn-line"><AlertTriangle size={13} /> Save this key now. It will <strong>not</strong> be shown again.</p>
						<div class="row">
							<code class="code key">{createdKey.recovery_key}</code>
							<button type="button" class="ed-btn sm" onclick={() => copy(createdKey!.recovery_key, 'rk')}>{#if copied === 'rk'}<Check size={13} /> Copied{:else}<Copy size={13} /> Copy{/if}</button>
						</div>
						<p class="dim">With this key and your Shuffle account you can restore the network onto a fresh worker when no other worker is online. Creating a new key replaces this one.</p>
						<label class="check">
							<input type="checkbox" bind:checked={keySaved} />
							<span>I saved this key somewhere safe.</span>
						</label>
						<button type="button" class="ed-btn sm primary" onclick={() => (createdKey = null)} disabled={!keySaved}>Done</button>
					</div>
				{:else}
					<p class="dim">
						{#if status.recovery?.configured}
							Created {relativeTime(status.recovery.created_at, now)} for key version {status.recovery.key_version}. Rotating replaces the old key immediately.
						{:else}
							Without a recovery key the network cannot be restored when every worker is lost. Create one and keep it with your other secrets.
						{/if}
					</p>
					<div class="actions">
						{#if status.recovery?.configured}
							{#if confirmRotate}
								<button type="button" class="ed-btn sm danger" onclick={makeRecoveryKey} disabled={recoveryBusy || !$isAdmin}>{recoveryBusy ? 'Rotating…' : 'Confirm rotate'}</button>
								<button type="button" class="ed-btn sm" onclick={() => (confirmRotate = false)} disabled={recoveryBusy}>Cancel</button>
							{:else}
								<button type="button" class="ed-btn sm" onclick={() => (confirmRotate = true)} disabled={!$isAdmin}><RefreshCw size={13} /> Rotate</button>
							{/if}
						{:else}
							<button type="button" class="ed-btn sm primary" onclick={makeRecoveryKey} disabled={recoveryBusy || !$isAdmin}>
								{#if recoveryBusy}<Loader2 size={13} class="spin" />{:else}<KeyRound size={13} />{/if} Create recovery key
							</button>
						{/if}
					</div>
				{/if}
			</div>

			<!-- ── (g) Sync ─────────────────────────────────────────────── -->
			<div class="block">
				<h5><Activity size={14} /> Sync</h5>
				<div class="stats">
					<div class="stat"><span class="n">{recordTotal}</span><span class="k">records</span></div>
					<div class="stat"><span class="n">{status.sync?.ops ?? 0}</span><span class="k">ops</span></div>
					<div class="stat"><span class="n">{status.sync?.holes?.length ?? 0}</span><span class="k">gaps</span></div>
					<div class="stat" class:bad={(status.sync?.conflicts ?? 0) > 0}><span class="n">{status.sync?.conflicts ?? 0}</span><span class="k">conflicts</span></div>
				</div>
				{#if Object.keys(status.sync?.records ?? {}).length > 0}
					<div class="records">
						{#each Object.entries(status.sync?.records ?? {}).sort(([a], [b]) => a.localeCompare(b)) as [collection, count] (collection)}
							<span class="chip">{collection} <strong>{count}</strong></span>
						{/each}
					</div>
				{/if}

				<h6>Peers</h6>
				{#if (status.sync?.peers?.length ?? 0) === 0}
					<p class="dim small">No other workers yet — invite one above.</p>
				{:else}
					<div class="list">
						{#each status.sync?.peers ?? [] as peer (peer.device_id)}
							<div class="mrow">
								<span class="dot" class:online={peer.online}></span>
								<div class="mmain">
									<div class="l1"><strong>{memberName(peer.device_id)}</strong><span class="dim">{peer.base_url ?? 'no address'}</span></div>
									<div class="l2"><span>last contact {contactAge(peer.seconds_since_contact)}</span>{#if peer.last_error}<span class="sep">·</span><span class="err">{peer.last_error}</span>{/if}</div>
								</div>
							</div>
						{/each}
					</div>
				{/if}

				{#if status.hub?.linked}
					<h6>Cloud</h6>
					<div class="l2 cloud">
						<span>{status.hub.registered ? 'registered' : 'not registered'}{status.hub.status ? ` · ${status.hub.status}` : ''}</span>
						<span class="sep">·</span><span>backlog {status.hub.backlog ?? 0}</span>
						<span class="sep">·</span><span>pushed {relativeTime(status.hub.last_push_at, now)}</span>
						<span class="sep">·</span><span>pulled {relativeTime(status.hub.last_pull_at, now)}</span>
					</div>
					{#if status.hub.last_error}<p class="err small">{status.hub.last_error}</p>{/if}
				{/if}

				{#if conflicts.length > 0}
					<h6>Conflicts</h6>
					<p class="dim small">Two workers changed the same record while apart. The newer change won; the losing copy is kept here for a while.</p>
					<div class="list">
						{#each conflicts as conflict (conflict.id)}
							<div class="mrow">
								<div class="mmain">
									<div class="l1"><strong>{conflict.collection}</strong><code class="small">{conflict.record_id}</code></div>
									<div class="l2"><span>won by {memberName(conflict.winning_device_id)}</span><span class="sep">·</span><span>lost by {memberName(conflict.losing_device_id)}</span><span class="sep">·</span><span>{relativeTime(conflict.created_at, now)}</span></div>
								</div>
								{#if $isAdmin}
									<button type="button" class="icon" title="Dismiss" onclick={() => dismissConflict(conflict.id)} disabled={dismissingConflict === conflict.id}><X size={13} /></button>
								{/if}
							</div>
						{/each}
					</div>
				{/if}

				<details class="policy" ontoggle={loadPolicy}>
					<summary>What syncs</summary>
					{#if policyLoading}
						<p class="dim small">Loading…</p>
					{:else if policy}
						<div class="table-wrap">
							<table>
								<thead><tr><th>Collection</th><th>Mode</th><th>Encrypted</th><th>Local only</th><th>Notes</th></tr></thead>
								<tbody>
									{#each policy.collections as c (c.collection)}
										<tr><td>{c.collection}</td><td>{c.mode}</td><td>{c.encrypted.join(', ') || '—'}</td><td>{c.local_fields.join(', ') || '—'}</td><td>{c.notes ?? ''}</td></tr>
									{/each}
								</tbody>
							</table>
						</div>
						<p class="dim small">Settings: {#each policy.settings as rule, i (rule.pattern)}{i > 0 ? ' · ' : ''}<code>{rule.pattern}</code> {rule.sharing}{/each}</p>
					{/if}
				</details>
			</div>
		{/if}
	{/if}

	{#if error}<p class="msg err" role="alert">{error}</p>{/if}
	{#if success}<p class="msg ok" role="status">{success}</p>{/if}
</section>

<style>
	.net-panel { margin: 8px 12px 12px; border: 1px solid color-mix(in oklch, var(--primary) 22%, var(--border)); border-radius: 13px; background: color-mix(in oklch, var(--primary) 3%, transparent); overflow: hidden; }
	.net-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; padding: 15px 16px 10px; }
	.net-title h4, h5 { display: flex; align-items: center; gap: 7px; margin: 0; font-size: 13.5px; }
	h5 { font-size: 12.5px; margin-bottom: 6px; }
	h6 { margin: 12px 0 4px; font-size: 10.5px; font-weight: 650; text-transform: uppercase; letter-spacing: .05em; color: var(--muted-foreground); }
	.net-title p, .dim { margin: 4px 0 0; color: var(--muted-foreground); font-size: 12px; line-height: 1.45; }
	.small { font-size: 11px; }
	.pill { flex: 0 0 auto; padding: 3px 9px; border-radius: 999px; font: 600 10px var(--font-mono, monospace); text-transform: uppercase; letter-spacing: .04em; }
	.pill.s-ready { color: var(--primary-foreground); background: var(--primary); }
	.pill.s-syncing { color: var(--foreground); background: color-mix(in oklch, var(--primary) 18%, transparent); }
	.pill.s-needs-approval, .pill.s-needs-setup { color: var(--warning-foreground, #92400e); background: color-mix(in oklch, var(--warning, #d97706) 20%, transparent); }
	.pill.s-offline { color: var(--muted-foreground); background: var(--muted); }
	.self-grid { display: grid; gap: 8px; padding: 0 16px 12px; }
	.kv { display: grid; grid-template-columns: 150px 1fr; gap: 8px; align-items: center; }
	.kv .k, .parsed .k { font-size: 10.5px; font-weight: 650; text-transform: uppercase; letter-spacing: .05em; color: var(--muted-foreground); }
	.kv .v { font-size: 12.5px; min-width: 0; }
	.row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
	.row .inp { flex: 1; min-width: 180px; }
	.name-in { width: 220px; max-width: 100%; }
	.fp { font-family: var(--font-mono, monospace); font-size: 12px; letter-spacing: .02em; padding: 2px 6px; border-radius: 6px; background: var(--muted); color: var(--foreground); word-break: break-all; }
	.fp.big { font-size: 13.5px; font-weight: 650; padding: 5px 8px; }
	.fp.small { font-size: 10.5px; margin-top: 3px; display: inline-block; }
	.code { font-family: var(--font-mono, monospace); font-size: 17px; font-weight: 750; letter-spacing: .08em; }
	.code.key { font-size: 15px; letter-spacing: .05em; padding: 6px 10px; border-radius: 8px; background: var(--muted); user-select: all; word-break: break-all; }
	code { font-family: var(--font-mono, monospace); font-size: 11.5px; }
	.block { border-top: 1px solid var(--border); padding: 12px 16px 14px; }
	.block.attention { background: color-mix(in oklch, var(--warning, #d97706) 7%, transparent); }
	.count { margin-left: 4px; padding: 2px 7px; border-radius: 999px; font: 600 10px var(--font-mono, monospace); text-transform: uppercase; color: var(--muted-foreground); background: var(--muted); }
	.list { display: flex; flex-direction: column; gap: 2px; margin-top: 4px; }
	.mrow { display: flex; align-items: center; gap: 10px; padding: 8px 8px; border-radius: 9px; }
	.mrow:hover { background: var(--hover-overlay); }
	.mrow.self { background: color-mix(in oklch, var(--primary) 6%, transparent); }
	.dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; background: var(--muted-foreground); }
	.dot.online { background: color-mix(in oklch, var(--primary) 70%, #22c55e); box-shadow: 0 0 5px color-mix(in oklch, var(--primary) 50%, #22c55e); }
	.mmain { flex: 1; min-width: 0; }
	.l1 { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; font-size: 13px; }
	.l2 { display: flex; align-items: center; gap: 5px; flex-wrap: wrap; margin-top: 2px; font-size: 11.5px; color: var(--muted-foreground); }
	.l2 .sep { opacity: .5; }
	.cloud { margin-top: 2px; }
	.b { font-family: var(--font-mono, monospace); font-size: 10px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; padding: 2px 7px; border-radius: 999px; }
	.b-on { color: var(--primary-foreground); background: var(--primary); }
	.b-muted { color: var(--muted-foreground); background: var(--muted); }
	.b-warn { color: var(--warning-foreground, #92400e); background: color-mix(in oklch, var(--warning, #d97706) 18%, transparent); }
	.icon { display: inline-grid; place-items: center; width: 28px; height: 28px; padding: 0; border: 0; border-radius: 7px; color: var(--muted-foreground); background: transparent; cursor: pointer; flex-shrink: 0; }
	.icon:hover:not(:disabled) { color: var(--foreground); background: var(--hover-overlay); }
	.icon:disabled { opacity: .5; cursor: default; }
	.confirm { max-width: 360px; padding: 8px 10px; border-radius: 9px; border: 1px solid color-mix(in oklch, var(--destructive) 30%, transparent); background: color-mix(in oklch, var(--destructive) 8%, transparent); }
	.confirm p { display: flex; gap: 6px; margin: 0 0 8px; font-size: 11.5px; line-height: 1.4; color: var(--foreground); }
	.confirm p :global(svg) { flex: 0 0 auto; margin-top: 2px; color: var(--destructive); }
	.actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 8px; }
	.approval { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 10px 10px; margin-top: 6px; border-radius: 10px; border: 1px solid var(--border); background: var(--background); }
	.amain { display: grid; gap: 4px; min-width: 0; font-size: 13px; }
	.check { display: flex; align-items: flex-start; gap: 8px; margin-top: 8px; font-size: 12px; line-height: 1.4; color: var(--foreground); cursor: pointer; }
	.check input { margin-top: 2px; }
	.grant { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; }
	.grant-main { display: grid; gap: 8px; flex: 1; min-width: 0; }
	.qr { width: 150px; height: 150px; padding: 7px; box-sizing: border-box; border-radius: 10px; background: white; flex-shrink: 0; }
	.qr :global(svg) { display: block; width: 100%; height: 100%; }
	.waiting { padding: 12px 14px; border-radius: 10px; border: 1px solid color-mix(in oklch, var(--primary) 30%, transparent); background: color-mix(in oklch, var(--primary) 6%, transparent); }
	.waiting h5 { margin-bottom: 4px; }
	.waiting p { margin: 6px 0; font-size: 12px; color: var(--muted-foreground); }
	.waiting .ed-btn { margin-top: 8px; }
	.fail { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; padding: 8px 10px; border-radius: 9px; font-size: 12px; color: var(--destructive); background: color-mix(in oklch, var(--destructive) 10%, transparent); border: 1px solid color-mix(in oklch, var(--destructive) 28%, transparent); }
	.fail.warn { color: var(--warning-foreground, var(--foreground)); background: color-mix(in oklch, var(--warning, #d97706) 12%, transparent); border-color: color-mix(in oklch, var(--warning, #d97706) 30%, transparent); }
	.fail span { flex: 1; min-width: 200px; line-height: 1.4; }
	.field { display: grid; gap: 4px; margin-top: 8px; }
	.field span { font-size: 10.5px; font-weight: 650; text-transform: uppercase; letter-spacing: .05em; color: var(--muted-foreground); }
	.field .inp { width: 100%; box-sizing: border-box; }
	.inp.mono { font-family: var(--font-mono, monospace); }
	.manual { display: grid; grid-template-columns: 1fr 1fr; gap: 0 12px; }
	.manual .wide { grid-column: 1 / -1; }
	.parsed { display: grid; gap: 4px; margin-top: 8px; padding: 8px 10px; border-radius: 9px; background: var(--muted); font-size: 12px; }
	.parsed div { display: grid; grid-template-columns: 150px 1fr; gap: 8px; align-items: center; }
	.divider { height: 1px; margin: 14px 0 12px; background: var(--border); }
	.reveal { padding: 10px 12px; border-radius: 10px; border: 1px solid color-mix(in oklch, var(--warning, #d97706) 40%, transparent); background: color-mix(in oklch, var(--warning, #d97706) 10%, transparent); }
	.reveal .ed-btn { margin-top: 8px; }
	.warn-line { display: flex; align-items: center; gap: 6px; margin: 0 16px 10px; font-size: 12px; color: var(--warning-foreground, var(--warning, #d97706)); }
	.reveal .warn-line { margin: 0 0 8px; }
	.stats { display: flex; gap: 10px; flex-wrap: wrap; margin: 4px 0 8px; }
	.stat { display: grid; min-width: 70px; padding: 6px 10px; border-radius: 9px; background: var(--muted); }
	.stat .n { font-size: 16px; font-weight: 700; }
	.stat .k { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted-foreground); }
	.stat.bad { background: color-mix(in oklch, var(--destructive) 12%, transparent); }
	.records { display: flex; gap: 6px; flex-wrap: wrap; }
	.chip { padding: 2px 8px; border-radius: 999px; font-size: 11px; color: var(--muted-foreground); background: var(--muted); }
	.err { color: var(--destructive); }
	.policy { margin-top: 12px; font-size: 12px; }
	.policy summary { cursor: pointer; color: var(--muted-foreground); font-weight: 600; }
	.table-wrap { overflow-x: auto; margin-top: 6px; }
	table { border-collapse: collapse; width: 100%; font-size: 11.5px; }
	th, td { text-align: left; padding: 4px 8px; border-bottom: 1px solid var(--border); vertical-align: top; }
	th { font-size: 10px; text-transform: uppercase; letter-spacing: .05em; color: var(--muted-foreground); }
	.muted { margin: 2px 16px 14px; display: flex; align-items: center; gap: 6px; color: var(--muted-foreground); font-size: 12px; }
	.msg { margin: 0 16px 12px; font-size: 12px; }
	.msg.err { color: var(--destructive); }
	.msg.ok { color: var(--success, #16a34a); }
	:global(.net-panel .spin) { animation: net-spin 1s linear infinite; }
	@keyframes net-spin { to { transform: rotate(360deg); } }
	@media (max-width: 620px) {
		.kv, .parsed div { grid-template-columns: 1fr; gap: 2px; }
		.grant { flex-direction: column-reverse; }
		.manual { grid-template-columns: 1fr; }
		.approval { flex-direction: column; }
	}
</style>
