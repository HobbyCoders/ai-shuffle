<script lang="ts">
	/**
	 * Cloud: what the AI Shuffle Cloud account gives this worker. Local use is
	 * free and never depends on it, and there is no license key: the plan comes
	 * from the account this worker is signed in with, so signing in on another
	 * worker gives it the same plan. The account only sets how many remote
	 * workers the hub serves. Three states, from `GET /api/v1/license`:
	 *
	 * - not_linked: what a free account adds, Link an account (the Devices
	 *   sign-in) and pricing.
	 * - free: 2 remote workers (or "N of 2 in use"), Upgrade.
	 * - paid: 4 remote workers by default or the account's seat count, how many
	 *   are in use when the hub says, and the renewal date.
	 *
	 * Replaces System > License; the endpoint keeps its `/license` name.
	 */
	import { onDestroy, onMount } from 'svelte';
	import { BookOpen, ExternalLink } from 'lucide-svelte';
	import {
		CLOUD_DOCS,
		CLOUD_PRICING_URL,
		FREE_WORKERS,
		getLicense,
		PAID_WORKERS,
		UNLIMITED_SEATS,
		type LicenseStatus
	} from '$lib/api/license';
	import { layout } from '$lib/stores/layout.svelte';
	import { settingsNav } from '$lib/settings/navigation.svelte';
	import { FORBIDDEN_TEXT, loadState } from '$lib/settings/access.svelte';
	import type { StatusTone } from '$lib/settings/types';
	import PreferencePage from '../shell/PreferencePage.svelte';
	import RowGroup from '../rows/RowGroup.svelte';
	import SettingRow from '../rows/SettingRow.svelte';
	import ValueText from '../rows/ValueText.svelte';
	import Meter from '../rows/Meter.svelte';
	import './editors/editors.css';

	let status = $state<LicenseStatus | null>(null);
	let load = $state<'loading' | 'ok' | 'forbidden' | 'error'>('loading');
	let error = $state('');
	let alive = true;

	const tier = $derived(status?.state ?? null);
	const paid = $derived(tier === 'paid');

	/** Remote workers the account allows: the paid seat count, else the limit, else the plan default. */
	const cap = $derived.by(() => {
		if (!status || tier === 'not_linked') return 0;
		if (paid) return status.seats || status.remote_worker_limit || PAID_WORKERS;
		return status.remote_worker_limit || FREE_WORKERS;
	});
	const unlimited = $derived(paid && cap >= UNLIMITED_SEATS);
	const used = $derived(status?.seats_used ?? null);

	const accountView = $derived.by((): { text: string; tone?: StatusTone } => {
		if (!status) {
			if (load === 'forbidden') return { text: FORBIDDEN_TEXT };
			return { text: load === 'error' ? 'Unavailable' : 'Loading…' };
		}
		if (tier === 'paid') return { text: 'Paid', tone: 'ok' };
		if (tier === 'free') return { text: 'Free', tone: 'ok' };
		return { text: 'Not linked', tone: 'off' };
	});

	const accountHint = $derived.by(() => {
		if (!status) return undefined;
		return status.linked ? 'Linked to AI Shuffle Cloud' : 'Local use stays free';
	});

	const workersHint = $derived.by(() => {
		if (tier === 'not_linked') return 'A free account adds two';
		if (unlimited && used !== null) return `${used} in use`;
		return undefined;
	});

	function shortDate(iso: string | null): string {
		if (!iso) return '';
		const date = new Date(iso);
		// A renewal is a calendar date: read it in UTC so it is not a day early west of UTC.
		return Number.isNaN(date.getTime())
			? iso
			: date.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
	}

	/* ---------- the three tiers, top of the page ---------- */

	const tiers = $derived([
		{ id: 'local', name: 'This worker', figure: 'Free', unit: 'Local use, no account' },
		{ id: 'free', name: 'Free account', figure: String(FREE_WORKERS), unit: 'remote workers, no card' },
		{
			id: 'paid',
			name: 'Paid plan',
			figure: unlimited ? 'Unlimited' : String(paid ? cap : PAID_WORKERS),
			unit: unlimited ? 'remote workers' : 'remote workers and sync'
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

	const linkAccount = () => settingsNav.go({ category: 'devices', item: 'account' });
	const openDocs = () => layout.openFeaturePage('docs', CLOUD_DOCS);

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
	<p class="st-msg" data-testid="cloud-follows">
		Your plan follows the Cloud account you sign in with. Sign in with the same account on another worker and it gets the same plan.
	</p>

	{#if status}
		<RowGroup title="Plan">
			<SettingRow id="cloud.remoteWorkers" hint={workersHint}>
				<span data-testid="cloud-seats">
					{#if tier === 'not_linked'}
						<ValueText value="None" />
					{:else if unlimited}
						<ValueText value="Unlimited remote workers" tone="fg" />
					{:else if used !== null}
						<Meter {used} {cap} unit=" in use" label="Remote workers in use" warnAt={1} />
					{:else}
						<ValueText value={`${cap} remote ${cap === 1 ? 'worker' : 'workers'} included`} tone="fg" />
					{/if}
				</span>
			</SettingRow>
			{#if paid && status.expires_at}
				<SettingRow id="cloud.renewal">
					<span data-testid="cloud-renewal"><ValueText value={shortDate(status.expires_at)} tone="fg" /></span>
				</SettingRow>
			{/if}
			{#if !paid}
				<SettingRow id="cloud.upgrade">
					<a class="st-btn" href={CLOUD_PRICING_URL} target="_blank" rel="noopener noreferrer">
						<ExternalLink />{tier === 'free' ? 'Upgrade' : 'See plans'}
					</a>
				</SettingRow>
			{/if}
		</RowGroup>
	{/if}
	{#if error}<p class="st-msg err" role="alert">{error}</p>{/if}
</PreferencePage>

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
		flex-wrap: wrap;
		align-items: baseline;
		gap: 2px 7px;
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
