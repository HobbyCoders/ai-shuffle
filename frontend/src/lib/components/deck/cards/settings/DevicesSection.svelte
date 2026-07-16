<script lang="ts">
	/**
	 * DevicesSection — the cluster registry panel.
	 *
	 * On a shared database every AI Shuffle install self-registers here. This panel
	 * lists those installs (online state, platform/version, reachability), lets the
	 * user rename or prune a device, and shows/rotates the shared cluster secret
	 * that authorizes cross-install control. Sessions from every install show up in
	 * the sidebar and any online install can be driven from here — send/interrupt
	 * ride the shared DB (runs lease + pending_interactions), so control needs no
	 * ``base_url``. A device's advertised address only governs whether its artifact
	 * links open on other devices.
	 */
	import { onMount } from 'svelte';
	import { MonitorSmartphone, RefreshCw, Pencil, Trash2, Check, X, KeyRound, Globe } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import {
		listInstalls,
		renameInstall,
		deleteInstall,
		getClusterSecretStatus,
		regenerateClusterSecret,
		getAdvertiseStatus,
		setAdvertiseUrl,
		testAdvertiseReachability,
		type Install,
		type ClusterSecretStatus,
		type AdvertiseStatus,
		type AdvertiseReachability
	} from '$lib/api/installs';

	let installs = $state<Install[]>([]);
	let selfId = $state('');
	let secret = $state<ClusterSecretStatus | null>(null);
	let advertise = $state<AdvertiseStatus | null>(null);
	let advertiseInput = $state('');
	let savingAdvertise = $state(false);
	let testingAdvertise = $state(false);
	let reachability = $state<AdvertiseReachability | null>(null);
	let loading = $state(true);
	let error = $state('');
	let success = $state('');

	// Inline rename state (one device at a time).
	let editingId = $state<string | null>(null);
	let editName = $state('');
	let savingId = $state<string | null>(null);

	// Delete confirmation (two-step, no native confirm dialog).
	let confirmDeleteId = $state<string | null>(null);

	let regenerating = $state(false);
	let confirmRegen = $state(false);

	const onlineCount = $derived(installs.filter((i) => i.online).length);

	onMount(load);

	async function load() {
		loading = true;
		error = '';
		try {
			const [resp, sec, adv] = await Promise.all([
				listInstalls(),
				getClusterSecretStatus(),
				getAdvertiseStatus()
			]);
			installs = resp.installs;
			selfId = resp.self_instance_id;
			secret = sec;
			advertise = adv;
			advertiseInput = adv.override || '';
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to load devices');
		} finally {
			loading = false;
		}
	}

	async function saveAdvertise() {
		const url = advertiseInput.trim();
		if (url && !/^https?:\/\//.test(url)) {
			error = 'URL must start with http:// or https://';
			return;
		}
		savingAdvertise = true;
		error = '';
		try {
			advertise = await setAdvertiseUrl(url);
			advertiseInput = advertise.override || '';
			// Reflect the new base_url on this install's row right away.
			installs = installs.map((i) => (i.id === selfId ? { ...i, base_url: advertise!.url } : i));
			reachability = null; // the address changed — any prior test result is stale
			flash(url ? 'Address override saved' : 'Reverted to auto-detected address');
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to save address');
		} finally {
			savingAdvertise = false;
		}
	}

	async function testAdvertise() {
		testingAdvertise = true;
		error = '';
		try {
			reachability = await testAdvertiseReachability();
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to test reachability');
		} finally {
			testingAdvertise = false;
		}
	}

	function flash(msg: string) {
		success = msg;
		setTimeout(() => (success = ''), 3000);
	}

	function startEdit(install: Install) {
		editingId = install.id;
		editName = install.device_name;
		confirmDeleteId = null;
	}

	function cancelEdit() {
		editingId = null;
		editName = '';
	}

	async function saveEdit(id: string) {
		const name = editName.trim();
		if (!name) return;
		savingId = id;
		error = '';
		try {
			const updated = await renameInstall(id, name);
			installs = installs.map((i) => (i.id === id ? { ...i, ...updated } : i));
			cancelEdit();
			flash('Device renamed');
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to rename device');
		} finally {
			savingId = null;
		}
	}

	async function remove(id: string) {
		savingId = id;
		error = '';
		try {
			await deleteInstall(id);
			installs = installs.filter((i) => i.id !== id);
			confirmDeleteId = null;
			flash('Device removed');
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to remove device');
		} finally {
			savingId = null;
		}
	}

	async function regenerate() {
		regenerating = true;
		error = '';
		try {
			const res = await regenerateClusterSecret();
			confirmRegen = false;
			if (res.env_pinned) {
				flash('Secret is pinned via CLUSTER_SECRET env — nothing to rotate');
			} else {
				flash('Cluster secret rotated. Restart the other installs so they pick it up.');
			}
			secret = await getClusterSecretStatus();
		} catch (e: unknown) {
			error = getErrorMessage(e, 'Failed to rotate cluster secret');
		} finally {
			regenerating = false;
		}
	}

	function relTime(input: string | null): string {
		if (!input) return 'never';
		const ms = new Date(input).getTime();
		if (!ms) return 'never';
		const diff = Date.now() - ms;
		const min = Math.floor(diff / 60000);
		const hr = Math.floor(diff / 3600000);
		const day = Math.floor(diff / 86400000);
		if (min < 1) return 'just now';
		if (min < 60) return `${min}m ago`;
		if (hr < 24) return `${hr}h ago`;
		if (day < 30) return `${day}d ago`;
		return new Date(ms).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
	}
</script>

<div class="set-group">
	<div class="set-grouphead">
		<h3><MonitorSmartphone />Devices
			<span class="b b-muted">{onlineCount}/{installs.length} online</span>
		</h3>
		<p>Every AI&nbsp;Shuffle install on this shared database registers itself here. Their sessions appear in your sidebar, and any <strong>online</strong> device can be controlled from here — no manual server setup. A device's advertised address only affects whether its artifact links open on your other devices.</p>
	</div>

	{#if loading}
		<div class="sg-body"><p class="muted">Loading…</p></div>
	{:else}
		<!-- Cluster secret -->
		<div class="set-row">
			<div class="lab">
				<h4><KeyRound size={13} /> Cluster secret</h4>
				<p>Shared key that authorizes installs to control each other. {#if secret?.env_pinned}Pinned via the <code>CLUSTER_SECRET</code> env var.{:else}Auto-generated and stored in the shared database.{/if}</p>
			</div>
			<div class="ctl secret-ctl">
				{#if secret?.configured}<span class="b b-on">Active</span>{:else}<span class="b b-muted">Not set</span>{/if}
				{#if confirmRegen}
					<button type="button" class="ed-btn danger" onclick={regenerate} disabled={regenerating}>
						{regenerating ? 'Rotating…' : 'Confirm rotate'}
					</button>
					<button type="button" class="ed-btn" onclick={() => (confirmRegen = false)} disabled={regenerating}>Cancel</button>
				{:else}
					<button
						type="button"
						class="ed-btn"
						onclick={() => (confirmRegen = true)}
						disabled={secret?.env_pinned}
						title={secret?.env_pinned ? 'Pinned via env — edit CLUSTER_SECRET to change' : 'Rotate the shared secret'}
					>
						<RefreshCw size={13} /> Rotate
					</button>
				{/if}
			</div>
		</div>

		<!-- This install's advertised address -->
		<div class="set-row">
			<div class="lab">
				<h4><Globe size={13} /> This install's address</h4>
				<p>The address other devices use to open artifact links served by this install. {#if advertise?.env_pinned}Pinned via <code>CLUSTER_ADVERTISE_URL</code>.{:else if advertise?.source === 'guess'}Auto-detected — override if other devices can't reach it.{:else if advertise && !advertise.url}Not advertised — artifact links from here may not open on your other devices.{/if}</p>
			</div>
			<div class="ctl secret-ctl">
				<input
					type="text"
					class="inp adv-in"
					bind:value={advertiseInput}
					placeholder={advertise?.url || 'https://this-install.example.com'}
					disabled={advertise?.env_pinned || savingAdvertise}
					autocomplete="off"
				/>
				<button
					type="button"
					class="ed-btn"
					onclick={testAdvertise}
					disabled={testingAdvertise || !advertise?.url}
					title={advertise?.url ? 'Check that the advertised address resolves and responds' : 'No address to test'}
				>{testingAdvertise ? 'Testing…' : 'Test'}</button>
				<button
					type="button"
					class="ed-btn primary"
					onclick={saveAdvertise}
					disabled={advertise?.env_pinned || savingAdvertise || advertiseInput.trim() === (advertise?.override || '')}
				>{savingAdvertise ? 'Saving…' : 'Save'}</button>
			</div>
		</div>

		<!-- Reachability warning + self-test result for the advertised address -->
		{#if !advertise?.env_pinned && advertise && !advertise.url}
			<div class="adv-warn" role="status">
				This install has no advertised address, so <strong>artifact links</strong>
				it serves may not open on your other devices. Set a
				<code>http://&lt;lan-ip&gt;:port</code> URL above so they resolve.
			</div>
		{/if}
		{#if reachability}
			<div class="adv-result" class:ok={reachability.ok} role="status">
				{reachability.detail}
			</div>
		{/if}

		<!-- Device list -->
		{#if installs.length === 0}
			<div class="sg-body"><p class="muted">No installs registered yet.</p></div>
		{:else}
			<div class="dev-list">
				{#each installs as inst (inst.id)}
					<div class="dev-row" class:self={inst.id === selfId}>
						<span class="dev-dot" class:online={inst.online} title={inst.online ? 'Online' : 'Offline'}></span>
						<div class="dev-main">
							<div class="dev-line1">
								{#if editingId === inst.id}
									<input
										class="inp dev-name-input"
										bind:value={editName}
										maxlength="60"
										onkeydown={(e) => { if (e.key === 'Enter') saveEdit(inst.id); if (e.key === 'Escape') cancelEdit(); }}
									/>
									<button type="button" class="ic-btn" title="Save" onclick={() => saveEdit(inst.id)} disabled={savingId === inst.id || !editName.trim()}><Check size={14} /></button>
									<button type="button" class="ic-btn" title="Cancel" onclick={cancelEdit} disabled={savingId === inst.id}><X size={14} /></button>
								{:else}
									<span class="dev-name">{inst.device_name}</span>
									{#if inst.id === selfId}<span class="b b-on">This install</span>{/if}
									{#if !inst.base_url}<span class="b b-muted" title="No address advertised — artifact links from this device may not open on your other devices">No address</span>{/if}
								{/if}
							</div>
							<div class="dev-line2">
								{#if inst.platform}<span>{inst.platform}</span>{/if}
								{#if inst.version}<span class="sep">·</span><span>v{inst.version}</span>{/if}
								<span class="sep">·</span>
								<span>{inst.online ? 'online' : `seen ${relTime(inst.last_seen)}`}</span>
							</div>
						</div>
						{#if editingId !== inst.id}
							<div class="dev-actions">
								<button type="button" class="ic-btn" title="Rename" onclick={() => startEdit(inst)}><Pencil size={13} /></button>
								{#if inst.id !== selfId}
									{#if confirmDeleteId === inst.id}
										<button type="button" class="ic-btn danger" title="Confirm remove" onclick={() => remove(inst.id)} disabled={savingId === inst.id}><Check size={14} /></button>
										<button type="button" class="ic-btn" title="Cancel" onclick={() => (confirmDeleteId = null)}><X size={14} /></button>
									{:else}
										<button type="button" class="ic-btn" title="Remove from registry" onclick={() => { confirmDeleteId = inst.id; editingId = null; }}><Trash2 size={13} /></button>
									{/if}
								{/if}
							</div>
						{/if}
					</div>
				{/each}
			</div>
		{/if}

		{#if error}<div class="kc-foot err">{error}</div>{/if}
		{#if success}<div class="kc-foot ok">{success}</div>{/if}
	{/if}
</div>

<style>
	.sg-body {
		padding: 14px 18px 16px;
	}
	.muted {
		margin: 0;
		font-size: 13px;
		color: var(--muted-foreground);
	}
	.secret-ctl {
		display: flex;
		align-items: center;
		gap: 8px;
		flex-wrap: wrap;
	}
	.adv-in {
		width: 280px;
		max-width: 100%;
	}
	.adv-warn,
	.adv-result {
		margin: 8px 12px 0;
		padding: 8px 12px;
		border-radius: 8px;
		font-size: 0.8125rem;
		line-height: 1.35;
	}
	.adv-warn {
		color: var(--warning-foreground, var(--warning));
		background: color-mix(in oklch, var(--warning) 12%, transparent);
		border: 1px solid color-mix(in oklch, var(--warning) 30%, transparent);
	}
	.adv-warn code {
		font-size: 0.75rem;
	}
	.adv-result {
		color: var(--destructive);
		background: color-mix(in oklch, var(--destructive) 10%, transparent);
		border: 1px solid color-mix(in oklch, var(--destructive) 28%, transparent);
	}
	.adv-result.ok {
		color: var(--success, #16a34a);
		background: color-mix(in oklch, var(--success, #16a34a) 12%, transparent);
		border-color: color-mix(in oklch, var(--success, #16a34a) 30%, transparent);
	}
	.dev-list {
		display: flex;
		flex-direction: column;
		padding: 4px 8px 8px;
	}
	.dev-row {
		display: flex;
		align-items: center;
		gap: 10px;
		padding: 10px 10px;
		border-radius: 10px;
	}
	.dev-row:hover {
		background: var(--hover-overlay);
	}
	.dev-row.self {
		background: color-mix(in oklch, var(--primary) 6%, transparent);
	}
	.dev-dot {
		width: 8px;
		height: 8px;
		border-radius: 50%;
		flex-shrink: 0;
		background: var(--muted-foreground);
	}
	.dev-dot.online {
		background: color-mix(in oklch, var(--primary) 70%, #22c55e);
		box-shadow: 0 0 5px color-mix(in oklch, var(--primary) 50%, #22c55e);
	}
	.dev-main {
		flex: 1;
		min-width: 0;
	}
	.dev-line1 {
		display: flex;
		align-items: center;
		gap: 7px;
		flex-wrap: wrap;
	}
	.dev-name {
		font-size: 13.5px;
		font-weight: 600;
		color: var(--foreground);
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.dev-name-input {
		width: 220px;
		max-width: 100%;
	}
	.dev-line2 {
		display: flex;
		align-items: center;
		gap: 5px;
		margin-top: 2px;
		font-size: 11.5px;
		color: var(--muted-foreground);
		text-transform: capitalize;
	}
	.dev-line2 .sep {
		opacity: 0.5;
	}
	.dev-actions {
		display: flex;
		align-items: center;
		gap: 2px;
		flex-shrink: 0;
	}
	.ic-btn {
		display: flex;
		align-items: center;
		justify-content: center;
		width: 28px;
		height: 28px;
		border: none;
		border-radius: 7px;
		background: transparent;
		color: var(--muted-foreground);
		cursor: pointer;
		transition: all 120ms ease;
	}
	.ic-btn:hover:not(:disabled) {
		background: var(--hover-overlay);
		color: var(--foreground);
	}
	.ic-btn.danger:hover:not(:disabled) {
		color: var(--destructive);
		background: color-mix(in oklch, var(--destructive) 12%, transparent);
	}
	.ic-btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.ed-btn.danger {
		color: var(--destructive);
		border-color: color-mix(in oklch, var(--destructive) 40%, transparent);
	}
	.b {
		font-family: var(--font-mono, monospace);
		font-size: 10px;
		font-weight: 600;
		letter-spacing: 0.04em;
		text-transform: uppercase;
		padding: 2px 7px;
		border-radius: 999px;
	}
	.b-on {
		color: var(--primary-foreground);
		background: var(--primary);
	}
	.b-muted {
		color: var(--muted-foreground);
		background: var(--muted);
	}
	.kc-foot {
		padding: 8px 18px 12px;
		font-size: 12.5px;
	}
	.kc-foot.ok {
		color: var(--success, #16a34a);
	}
	.kc-foot.err {
		color: var(--destructive);
	}
	code {
		font-family: var(--font-mono, monospace);
		font-size: 11px;
		padding: 1px 4px;
		border-radius: 4px;
		background: var(--muted);
	}
</style>
