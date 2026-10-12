<script lang="ts">
	/**
	 * Cloud: what the AI Shuffle Cloud account gives this worker. Local use is
	 * free and never depends on it; the account only sets how many remote
	 * workers the hub serves (none, one on a free account, the plan's seats on
	 * a paid one). Three states, from `GET /api/v1/license`:
	 *
	 * - not_linked: what a free account adds, Link an account (the Devices
	 *   sign-in), pricing, and the paid key entry.
	 * - free: 1 remote worker, Upgrade, the key entry.
	 * - paid: plan, seats, paid access date with Check now, the key and Deactivate.
	 *
	 * A saved activation can outlive paid access: after expiry, an invalid key
	 * or a seat freed elsewhere the state falls back to free or not_linked while
	 * the key stays on this worker. Check now and Deactivate stay offered then,
	 * so the owner can renew or forget it.
	 *
	 * Replaces System > License; the endpoints keep their `/license` names.
	 * A plan key, like every secret, needs an explicit Activate.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { BookOpen, ExternalLink, RefreshCw } from 'lucide-svelte';
	import { getErrorMessage } from '$lib/api/client';
	import {
		activateLicense,
		CLOUD_DOCS,
		CLOUD_PRICING_URL,
		deactivateLicense,
		getLicense,
		planName,
		refreshLicense,
		releaseSeat,
		type LicenseStatus,
		type SeatHolder
	} from '$lib/api/license';
	import { layout } from '$lib/stores/layout.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { flashSaved } from '$lib/settings/save-status.svelte';
	import { FORBIDDEN_TEXT, loadState } from '$lib/settings/access.svelte';
	import type { StatusTone } from '$lib/settings/types';
	import PreferencePage from '../shell/PreferencePage.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import ExpandToggle from '../rows/ExpandToggle.svelte';
	import InlineConfirm from '../rows/InlineConfirm.svelte';
	import Meter from '../rows/Meter.svelte';
	import PlanKeyForm from './editors/PlanKeyForm.svelte';
	import SeatHolders from './editors/SeatHolders.svelte';
	import './editors/editors.css';

	/** Remote workers on the paid plan when the status does not say. */
	const PAID_WORKERS = 3;

	let status = $state<LicenseStatus | null>(null);
	let load = $state<'loading' | 'ok' | 'forbidden' | 'error'>('loading');
	let key = $state('');
	let busy = $state(false);
	let error = $state('');
	let notice = $state('');
	let seatHolders = $state<SeatHolder[] | null>(null);
	let keyOpen = $state(false);
	let seatsOpen = $state(false);
	let alive = true;

	const tier = $derived(status?.state ?? null);
	const paid = $derived(tier === 'paid');
	/** A paid lease this worker holds and that still grants paid access. */
	const hasLease = $derived(paid && !!status && ['active', 'grace', 'expiring', 'ending'].includes(status.lease_state));
	/** A key is saved on this worker, paid or not: Check now and Deactivate apply. */
	const hasActivation = $derived(!!status && (!!status.key_hint || status.lease_state !== 'none'));
	/** The saved key no longer grants paid access (expired, invalid, seat lost). */
	const lapsed = $derived(hasActivation && !paid);
	const leaseTrouble = $derived(!!status && ['grace', 'expiring', 'ending'].includes(status.lease_state));
	const keyEntry = $derived(!!status && (status.server_configured || !!status.key_hint));
	const limit = $derived(status?.remote_worker_limit ?? 0);

	const accountView = $derived.by((): { text: string; tone?: StatusTone } => {
		if (!status) {
			if (load === 'forbidden') return { text: FORBIDDEN_TEXT };
			return { text: load === 'error' ? 'Unavailable' : 'Loading…' };
		}
		if (tier === 'paid') return { text: 'Paid', tone: leaseTrouble ? 'warn' : 'ok' };
		if (tier === 'free') return { text: 'Free', tone: 'ok' };
		return { text: 'Not linked', tone: 'off' };
	});

	const accountHint = $derived.by(() => {
		if (!status) return undefined;
		if (!status.linked) return paid ? 'Plan key, account not linked' : 'Local use stays free';
		return 'Linked to AI Shuffle Cloud';
	});

	function shortDate(iso: string | null): string {
		if (!iso) return '';
		const date = new Date(iso);
		return Number.isNaN(date.getTime()) ? iso : date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
	}

	function ago(iso: string | null): string {
		if (!iso) return 'Never checked';
		const t = new Date(iso).getTime();
		if (Number.isNaN(t)) return 'Checked';
		const minutes = Math.max(0, Math.round((Date.now() - t) / 60_000));
		if (minutes < 1) return 'Checked just now';
		if (minutes < 60) return `Checked ${minutes} min ago`;
		const hours = Math.round(minutes / 60);
		if (hours < 48) return `Checked ${hours} h ago`;
		return `Checked ${shortDate(iso)}`;
	}

	const planHint = $derived.by(() => {
		if (!status) return undefined;
		if (status.lease_state === 'grace') return status.grace_until ? `Payment grace until ${shortDate(status.grace_until)}` : 'Payment grace period';
		if (status.lease_state === 'expiring') return 'Renewal overdue';
		if (status.lease_state === 'ending') return 'Ends at period end';
		return undefined;
	});

	const LAPSED_TEXT: Record<string, string> = {
		invalid: 'Key not accepted',
		seat_lost: 'Seat freed elsewhere'
	};
	const renewalValue = $derived.by(() => {
		if (!status) return '';
		if (lapsed) {
			if (status.lease_state === 'expired') return status.expires_at ? `Ended ${shortDate(status.expires_at)}` : 'Ended';
			return LAPSED_TEXT[status.lease_state] ?? 'Not active';
		}
		return status.expires_at ? shortDate(status.expires_at) : 'Not set';
	});
	const renewalHint = $derived(status?.last_error ? 'Last check failed' : ago(status?.last_refresh_at ?? null));

	/* ---------- the three tiers, top of the page ---------- */

	const tiers = $derived([
		{ id: 'local', name: 'This worker', figure: 'Free', unit: 'Local use, no account' },
		{ id: 'free', name: 'Free account', figure: '1', unit: 'remote worker, no card' },
		{
			id: 'paid',
			name: 'Paid plan',
			figure: String(paid && status?.seats ? status.seats : PAID_WORKERS),
			unit: 'remote workers and sync'
		}
	]);
	const currentTier = $derived(tier === 'paid' ? 'paid' : tier === 'free' ? 'free' : tier ? 'local' : null);
	const tierIndex = $derived(tiers.findIndex((t) => t.id === currentTier));

	/* ---------- loading and actions ---------- */

	async function loadStatus() {
		const result = await loadState(getLicense);
		if (!alive) return;
		if (result.status === 'ok') {
			status = result.data;
			load = 'ok';
		} else {
			load = result.status;
			if (result.status === 'error') error = result.message;
		}
	}

	async function run(action: () => Promise<void>) {
		if (busy) return;
		busy = true;
		error = '';
		notice = '';
		try {
			await action();
		} catch (cause) {
			error = getErrorMessage(cause, 'AI Shuffle Cloud could not be reached. Local use is unaffected.');
		} finally {
			busy = false;
		}
	}

	const activate = () =>
		run(async () => {
			const result = await activateLicense(key.trim());
			if (!result.activated) {
				seatHolders = result.installs;
				seatsOpen = true;
				error = result.detail;
				return;
			}
			const { activated: _activated, ...next } = result;
			seatHolders = null;
			key = '';
			keyOpen = false;
			notice = 'Paid plan active on this worker.';
			flashSaved();
			status = next;
		});

	const freeSeat = (holder: SeatHolder) =>
		run(async () => {
			await releaseSeat(key.trim(), holder.install_id);
			seatHolders = seatHolders?.filter((h) => h.install_id !== holder.install_id) ?? null;
			notice = `Freed the seat of ${holder.name || 'that worker'}. Activate again to use it here.`;
		});

	const checkNow = () =>
		run(async () => {
			status = await refreshLicense();
			notice = status.last_error ? '' : 'Checked with AI Shuffle Cloud.';
		});

	const deactivate = () =>
		run(async () => {
			const result = await deactivateLicense();
			const { seat_released: released, ...next } = result;
			notice = released
				? 'Paid plan removed from this worker and its seat freed. Local use is unaffected.'
				: 'Paid plan removed from this worker. Cloud could not be reached, so the seat frees itself after 21 days.';
			status = next;
		});

	const linkAccount = () => settingsNav.go({ category: 'devices', item: 'account' });
	const openDocs = () => layout.openFeaturePage('docs', CLOUD_DOCS);

	const seatsValue = $derived(
		seatHolders?.length && !hasLease
			? `${seatHolders.length} in use`
			: `${status?.seats_used ?? '?'} of ${status?.seats ?? PAID_WORKERS}`
	);

	onMount(() => void loadStatus());
	onDestroy(() => {
		alive = false;
	});
</script>

<PreferencePage category="cloud">
	<section class="cl-ladder" aria-label="What AI Shuffle Cloud adds" aria-busy={load === 'loading'} data-testid="cloud-tiers">
		<ol>
			{#each tiers as t, i (t.id)}
				<li
					class="cl-tier"
					class:current={t.id === currentTier}
					class:reached={tierIndex >= 0 && i < tierIndex}
					aria-current={t.id === currentTier ? 'true' : undefined}
				>
					<span class="cl-name">{t.name}</span>
					<span class="cl-fig"><b>{t.figure}</b><span>{t.unit}</span></span>
					{#if t.id === currentTier}<span class="cl-here">You are here</span>{/if}
				</li>
			{/each}
		</ol>
		<div class="cl-foot">
			<p class="cl-msg" data-testid="cloud-message">
				{status?.message ?? 'Everything on your own machines and network is free.'}
			</p>
			<button type="button" class="st-btn ghost sm" onclick={openDocs}><BookOpen />How Cloud works</button>
		</div>
	</section>

	<RowGroup title="Account">
		<SettingRow id="cloud.account" hint={accountHint}>
			<span data-testid="cloud-state"><ValueText value={accountView.text} status={accountView.tone} /></span>
		</SettingRow>
		{#if status && !status.linked}
			<SettingRow id="cloud.link">
				<button type="button" class="st-btn primary" onclick={linkAccount}>Link account</button>
			</SettingRow>
		{/if}
	</RowGroup>

	{#if status}
		<RowGroup title="Plan">
			<SettingRow
				id="cloud.remoteWorkers"
				hint={tier === 'not_linked' && !seatHolders?.length ? 'A free account adds one' : undefined}
				expanded={seatsOpen && seatHolders?.length ? seatList : undefined}
			>
				{#if seatHolders?.length}
					<ExpandToggle value={seatsValue} bind:open={seatsOpen} />
				{:else if paid && status.seats_used !== null && status.seats}
					<span data-testid="cloud-seats"><Meter used={status.seats_used} cap={status.seats} label="Remote workers in use" warnAt={1} /></span>
				{:else if paid}
					<span data-testid="cloud-seats"><ValueText value={seatsValue} tone="fg" /></span>
				{:else}
					<span data-testid="cloud-seats">
						<ValueText
							value={limit > 0 ? `${limit} remote ${limit === 1 ? 'worker' : 'workers'} included` : 'None'}
							tone={limit > 0 ? 'fg' : 'muted'}
						/>
					</span>
				{/if}
			</SettingRow>
			{#if paid}
				<SettingRow id="cloud.plan" hint={planHint}>
					<ValueText value={planName(status.plan)} tone="fg" status={leaseTrouble ? 'warn' : undefined} />
				</SettingRow>
			{/if}
			{#if paid || hasActivation}
				<SettingRow id="cloud.renewal" hint={renewalHint}>
					<span data-testid="cloud-renewal"><ValueText value={renewalValue} status={lapsed ? 'warn' : undefined} /></span>
					{#if hasActivation}
						<button type="button" class="st-btn sm" aria-label="Check the plan now" disabled={busy} onclick={checkNow}>
							<RefreshCw />Check now
						</button>
					{/if}
				</SettingRow>
			{/if}
			{#if !paid}
				<SettingRow id="cloud.upgrade">
					<a class="st-btn" href={CLOUD_PRICING_URL} target="_blank" rel="noopener noreferrer">
						<ExternalLink />{tier === 'free' ? 'Upgrade' : 'See plans'}
					</a>
				</SettingRow>
			{/if}
			{#if keyEntry}
				<SettingRow
					id="cloud.key"
					hint={hasActivation ? undefined : 'For a paid plan'}
					expanded={keyOpen ? keyForm : undefined}
				>
					<ExpandToggle
						value={status.key_hint ? `ASH-…${status.key_hint}` : 'Add key'}
						mono={!!status.key_hint}
						bind:open={keyOpen}
					/>
				</SettingRow>
			{/if}
			{#if hasActivation}
				<SettingRow id="cloud.deactivate" hint={lapsed ? 'Forgets the saved key' : undefined}>
					<InlineConfirm
						label="Deactivate"
						question={lapsed ? 'Forget the saved plan key?' : "Free this worker's paid seat?"}
						confirmLabel="Deactivate"
						disabled={busy}
						onConfirm={deactivate}
					/>
				</SettingRow>
			{/if}
		</RowGroup>
		{#if status.last_error}<p class="st-msg" role="status">Last check failed: {status.last_error}</p>{/if}
	{/if}
	{#if notice}<p class="st-msg" role="status">{notice}</p>{/if}
	{#if error}<p class="st-msg err" role="alert">{error}</p>{/if}
</PreferencePage>

{#snippet keyForm()}
	<PlanKeyForm replacing={hasActivation} {busy} bind:key onActivate={activate} />
{/snippet}

{#snippet seatList()}
	<SeatHolders holders={seatHolders ?? []} {busy} onFree={freeSeat} />
{/snippet}

<style>
	/* The tier ladder: three steps, the current one lifted out of the row. */
	.cl-ladder {
		container-type: inline-size;
		margin-bottom: 28px;
	}
	ol {
		list-style: none;
		margin: 0;
		padding: 0;
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		border: 1px solid var(--st-border);
		border-radius: var(--st-radius);
		background: var(--st-card);
	}
	.cl-tier {
		position: relative;
		display: grid;
		align-content: start;
		gap: 6px;
		padding: 14px 16px 16px;
		min-width: 0;
		color: var(--st-mfg);
		transition:
			background-color 0.25s ease-out,
			color 0.25s ease-out;
	}
	.cl-tier + .cl-tier {
		border-left: 1px solid var(--st-border);
	}
	.cl-tier::before {
		/* the progress rail along the top edge */
		content: '';
		position: absolute;
		inset: -1px 0 auto;
		height: 3px;
		background: transparent;
		transition: background-color 0.25s ease-out;
	}
	.cl-tier:first-child::before {
		border-top-left-radius: var(--st-radius);
	}
	.cl-tier:last-child::before {
		border-top-right-radius: var(--st-radius);
	}
	.cl-tier.reached::before {
		background: var(--st-primary-line);
	}
	.cl-tier.current {
		background: var(--st-primary-soft);
		color: var(--st-fg);
	}
	.cl-tier.current::before {
		background: var(--st-primary);
	}
	.cl-name {
		font-size: 12px;
		font-weight: 500;
		letter-spacing: 0.04em;
		text-transform: uppercase;
	}
	.cl-fig {
		display: flex;
		align-items: baseline;
		gap: 7px;
		min-width: 0;
	}
	.cl-fig b {
		font-size: 26px;
		line-height: 1;
		font-weight: 600;
		letter-spacing: -0.02em;
		color: var(--st-fg);
		font-variant-numeric: tabular-nums;
	}
	.cl-tier:not(.current) .cl-fig b {
		color: var(--st-mfg);
	}
	.cl-fig span {
		font-size: 12.5px;
		line-height: 1.3;
	}
	.cl-here {
		justify-self: start;
		font-size: 11px;
		font-weight: 500;
		padding: 2px 7px;
		border-radius: 999px;
		color: var(--st-primary);
		border: 1px solid var(--st-primary-line);
		animation: cl-in 0.3s ease-out;
	}
	.cl-foot {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
		margin-top: 10px;
	}
	.cl-msg {
		margin: 0;
		font-size: 13px;
		color: var(--st-mfg);
		min-width: 0;
	}
	@keyframes cl-in {
		from {
			opacity: 0;
			transform: translateY(2px);
		}
	}
	@media (prefers-reduced-motion: reduce) {
		.cl-tier,
		.cl-tier::before {
			transition: none;
		}
		.cl-here {
			animation: none;
		}
	}
	/* Narrow (phone): the steps stack, rail on the left. */
	@container (max-width: 480px) {
		ol {
			grid-template-columns: minmax(0, 1fr);
		}
		.cl-tier {
			grid-template-columns: minmax(0, 1fr) auto;
			align-items: center;
			padding: 12px 14px;
		}
		.cl-tier .cl-name {
			grid-column: 1;
		}
		.cl-tier .cl-fig {
			grid-column: 1;
		}
		.cl-tier .cl-here {
			grid-column: 2;
			grid-row: 1 / span 2;
			align-self: center;
		}
		.cl-tier + .cl-tier {
			border-left: 0;
			border-top: 1px solid var(--st-border);
		}
		.cl-tier::before {
			inset: 0 auto 0 -1px;
			width: 3px;
			height: auto;
		}
		.cl-tier:first-child::before {
			border-radius: var(--st-radius) 0 0 0;
		}
		.cl-tier:last-child::before {
			border-radius: 0 0 0 var(--st-radius);
		}
		.cl-foot {
			flex-direction: column;
			align-items: flex-start;
			gap: 6px;
		}
	}
</style>
