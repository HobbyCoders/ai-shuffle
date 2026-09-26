<script lang="ts">
	import { onMount } from 'svelte';
	import { BadgeCheck } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import {
		activateLicense, deactivateLicense, getLicense, LICENSE_CHANGED_EVENT, planName, refreshLicense, releaseSeat,
		type LicenseStatus, type SeatHolder
	} from '$lib/api/license';
	import Banner from '$lib/components/Banner.svelte';

	interface Props { isAdmin?: boolean }
	let { isAdmin = false }: Props = $props();

	let status = $state<LicenseStatus | null>(null);
	let key = $state('');
	let busy = $state(false);
	let error = $state('');
	let notice = $state('');
	let seatHolders = $state<SeatHolder[] | null>(null);
	let alive = false;

	const STATE_LABELS: Record<string, string> = {
		beta: 'Beta', active: 'Active', trialing: 'Trial', grace: 'Payment grace period', expiring: 'Renewal overdue',
		ending: 'Ending', expired: 'Expired', unlicensed: 'Not activated', invalid: 'Needs attention'
	};
	const hasLease = $derived(!!status && status.mode === 'licensed' && !['unlicensed', 'invalid'].includes(status.state));

	function when(iso: string | null): string {
		if (!iso) return 'Never';
		const date = new Date(iso);
		return Number.isNaN(date.getTime()) ? iso : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
	}

	async function load() {
		try {
			const next = await getLicense();
			if (alive) status = next;
		} catch (cause) {
			if (alive) error = getErrorMessage(cause, 'Could not load the license status');
		}
	}

	function changed(next: LicenseStatus) {
		status = next;
		window.dispatchEvent(new Event(LICENSE_CHANGED_EVENT));
	}

	async function run(action: () => Promise<void>) {
		if (busy) return;
		busy = true; error = ''; notice = '';
		try { await action(); } catch (cause) { error = getErrorMessage(cause, 'The license server could not be reached'); }
		finally { busy = false; }
	}

	const activate = () => run(async () => {
		const result = await activateLicense(key.trim());
		if (!result.activated) {
			seatHolders = result.installs;
			error = result.detail;
			return;
		}
		const { activated: _, ...next } = result;
		seatHolders = null;
		key = '';
		notice = 'This worker is licensed.';
		changed(next);
	});

	const free = (holder: SeatHolder) => run(async () => {
		await releaseSeat(key.trim(), holder.install_id);
		seatHolders = seatHolders?.filter((h) => h.install_id !== holder.install_id) ?? null;
		notice = `Freed the seat of ${holder.name || 'that worker'}. Activate again to use it here.`;
	});

	const deactivate = () => run(async () => {
		if (!window.confirm('Deactivate the license on this worker? Its seat is freed for another worker. New agent runs pause here until you activate again; your sessions and files are not affected.')) return;
		const result = await deactivateLicense();
		notice = result.seat_released ? 'License deactivated and the seat freed.' : 'License removed from this worker. The license server could not be reached, so the seat frees itself after 21 days.';
		changed(result);
	});

	const check = () => run(async () => { changed(await refreshLicense()); notice = 'Checked with the license server.'; });

	onMount(() => {
		alive = true;
		void load();
		return () => { alive = false; };
	});
</script>

<div class="set-group">
	<div class="set-grouphead"><h3><BadgeCheck />License</h3></div>
	{#if !status}
		<div class="set-row"><p class="hint">{error || 'Loading…'}</p></div>
	{:else if status.mode === 'beta'}
		<div class="set-row stack">
			<p class="lead" data-testid="license-beta">Beta: no license needed.</p>
			<p class="hint">Every feature works while the beta runs. When paid licenses start, you'll add your key here.</p>
		</div>
	{:else}
		{#if status.banner}
			<div class="set-row"><Banner severity={status.banner.severity} message={status.banner.message} /></div>
		{/if}
		<dl class="facts">
			<div><dt>Status</dt><dd data-testid="license-state">{STATE_LABELS[status.state] ?? status.state}</dd></div>
			{#if hasLease}
				<div><dt>Plan</dt><dd>{planName(status.plan)}</dd></div>
				<div><dt>Workers</dt><dd data-testid="license-seats">{status.seats_used ?? '?'} of {status.seats ?? 3} in use</dd></div>
				<div><dt>{status.state === 'expired' ? 'Lease ended' : 'Lease valid until'}</dt><dd>{when(status.expires_at)}</dd></div>
				{#if status.grace_until}<div><dt>Payment grace until</dt><dd>{when(status.grace_until)}</dd></div>{/if}
				{#if status.key_hint}<div><dt>Key</dt><dd class="mono">…{status.key_hint}</dd></div>{/if}
				<div><dt>Last checked</dt><dd>{when(status.last_refresh_at)}</dd></div>
			{/if}
		</dl>
		{#if status.last_error}<p class="hint row-pad">Last check failed: {status.last_error}</p>{/if}
		{#if !status.allowed && status.reason}<p class="hint row-pad">{status.reason}</p>{/if}

		{#if isAdmin}
			<div class="set-row stack">
				<div class="lab"><h4>{hasLease ? 'Use a different key' : 'License key'}</h4></div>
				<div class="ctl key-row">
					<input
						class="inp mono"
						aria-label="License key"
						placeholder="ASH-XXXXX-XXXXX-XXXXX-XXXXX"
						autocomplete="off"
						spellcheck="false"
						bind:value={key}
						onkeydown={(e) => { if (e.key === 'Enter' && key.trim()) void activate(); }}
					/>
					<button type="button" class="ed-btn primary" disabled={busy || !key.trim()} onclick={activate}>{busy ? '…' : 'Activate'}</button>
				</div>
				{#if seatHolders?.length}
					<div class="seats" aria-label="Workers using this license">
						<p class="hint">Free a seat on a worker you no longer use, then activate again.</p>
						<ul>
							{#each seatHolders as holder (holder.install_id)}
								<li>
									<span>{holder.name || holder.install_id}<small> · last seen {when(holder.last_seen)}{holder.app_version ? ` · ${holder.app_version}` : ''}</small></span>
									<button type="button" class="ed-btn" disabled={busy} onclick={() => free(holder)}>Deactivate</button>
								</li>
							{/each}
						</ul>
					</div>
				{/if}
			</div>
			{#if hasLease}
				<div class="set-row actions">
					<button type="button" class="ed-btn" disabled={busy} onclick={check}>Check now</button>
					<button type="button" class="ed-btn" disabled={busy} onclick={deactivate}>Deactivate on this worker</button>
				</div>
			{/if}
		{/if}
		<p class="hint row-pad">One license covers up to 3 workers. Without an active license, new agent runs pause; your sessions, files and settings always stay available.</p>
	{/if}
	{#if notice}<p class="msg ok row-pad" role="status">{notice}</p>{/if}
	{#if error && status}<p class="msg err row-pad" role="alert">{error}</p>{/if}
</div>

<style>
	.facts { margin: 0; padding: 4px 14px; display: grid; gap: 6px; }
	.facts div { display: flex; justify-content: space-between; gap: 16px; font-size: 13px; }
	.facts dt { color: var(--muted-foreground); }
	.facts dd { margin: 0; text-align: right; }
	.mono { font-family: var(--font-mono); }
	.lead { margin: 0; font-size: 13px; font-weight: 600; }
	.hint { margin: 0; color: var(--muted-foreground); font-size: 12px; line-height: 1.5; }
	.row-pad { padding: 4px 14px 10px; }
	.key-row { display: flex; gap: 8px; flex-wrap: wrap; }
	.key-row input { flex: 1; min-width: 220px; }
	.actions { display: flex; gap: 8px; flex-wrap: wrap; }
	.seats ul { list-style: none; padding: 0; margin: 8px 0 0; display: grid; gap: 6px; }
	.seats li { display: flex; justify-content: space-between; align-items: center; gap: 12px; font-size: 13px; }
	.seats small { color: var(--muted-foreground); }
	.msg { margin: 0; font-size: 12.5px; }
	.msg.ok { color: var(--success, #16a34a); }
	.msg.err { color: var(--destructive); }
</style>
