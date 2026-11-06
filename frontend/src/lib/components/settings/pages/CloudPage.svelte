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

	const accountHint = $derived(status?.linked ? 'Linked to AI Shuffle Cloud' : undefined);

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
		{ id: 'free', name: 'Free account', figure: String(tier === 'free' ? cap : FREE_WORKERS), unit: 'remote workers, no card' },
		{
			id: 'paid',
			name: 'Paid plan',
			figure: unlimited ? 'Unlimited' : String(paid ? cap : PAID_WORKERS),
			unit: unlimited ? 'remote workers' : 'remote workers and sync'
		}
	]);
	const currentTier = $derived(tier === 'paid' ? 'paid' : tier === 'free' ? 'free' : tier ? 'local' : null);

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
	<RowGroup title="Account">
		<SettingRow id="cloud.account" hint={accountHint} stackOnPhone={!!status && !status.linked}>
			<span data-testid="cloud-state"><ValueText value={accountView.text} status={accountView.tone} /></span>
			{#if status && !status.linked}
				<button type="button" class="st-btn primary" data-setting-anchor="cloud.link" onclick={linkAccount}>Link account</button>
			{/if}
		</SettingRow>
	</RowGroup>

	<section aria-label="Plans" aria-busy={load === 'loading'} data-testid="cloud-tiers">
		<RowGroup title="Plans">
			{#each tiers as t (t.id)}
				<SettingRow label={t.name} hint={`${t.figure} · ${t.unit}`} stackOnPhone={t.id === 'paid' || (t.id === currentTier && used !== null)}>
					{#snippet badge()}
						{#if t.id === currentTier}<span class="cl-here" aria-current="true">You are here</span>{/if}
					{/snippet}
					{#if t.id === currentTier && t.id !== 'local'}
						<span class="cl-seats" data-setting-anchor="cloud.remoteWorkers">
							{#if used !== null && !unlimited}
								<!-- Phone only: the meter's own line reads "Remote workers … 3 of 7 in use". -->
								<span class="cl-seats-label" aria-hidden="true">Remote workers</span>
							{/if}
							<span class="cl-seats-value" data-testid="cloud-seats">
								{#if unlimited}
									<ValueText value={used !== null ? `${used} in use` : 'Unlimited remote workers'} />
								{:else if used !== null}
									<Meter {used} {cap} unit=" in use" label="Remote workers in use" warnAt={1} />
								{:else}
									<span class="sr-only">{cap} remote {cap === 1 ? 'worker' : 'workers'} included</span>
								{/if}
							</span>
						</span>
					{/if}
					{#if t.id === 'paid'}
						<a class="st-btn phone-action" data-setting-anchor="cloud.upgrade" href={CLOUD_PRICING_URL} target="_blank" rel="noopener noreferrer"><ExternalLink />See plans</a>
					{/if}
				</SettingRow>
			{/each}
			{#if paid && status?.expires_at}
				<SettingRow id="cloud.renewal">
					<span data-testid="cloud-renewal"><ValueText value={shortDate(status.expires_at)} /></span>
				</SettingRow>
			{/if}
		</RowGroup>
	</section>
	<div class="cl-foot st-footnote">
		<p class="cl-msg" data-testid="cloud-follows">Your plan follows your Cloud account across workers. Local use stays free.</p>
		<button type="button" class="st-btn ghost sm" onclick={openDocs}><BookOpen />How Cloud works</button>
	</div>
	{#if error}<p class="st-msg err" role="alert">{error} <button type="button" class="st-btn" onclick={() => { error = ''; load = 'loading'; void loadStatus(); }}>Try again</button></p>{/if}
</PreferencePage>

<style>
	/* A small badge beside the plan name. */
	.cl-here { flex: none; font-size: 11.5px; font-weight: 400; line-height: 16px; padding: 0 7px; border: 1px solid var(--st-border-strong); border-radius: 999px; color: var(--st-mfg); white-space: nowrap; }
	.cl-seats-label { display: none; }

	/* Phone: the meter takes its own full-width line (label left, count right,
	   bar below) and See plans becomes a full-width button 12px under it. */
	:global(.settings-shell[data-stacked]) .cl-seats { display: grid; width: 100%; }
	:global(.settings-shell[data-stacked]) .cl-seats > :global(*) { grid-area: 1 / 1; }
	:global(.settings-shell[data-stacked]) .cl-seats-label { display: block; align-self: start; font-size: 12.5px; line-height: 18px; color: var(--st-mfg); }
	:global(.settings-shell[data-stacked]) .cl-seats-value { display: grid; }
	:global(.settings-shell[data-stacked]) .cl-seats :global(.st-meter) { width: 100%; }
	:global(.settings-shell[data-stacked] .st-row.st-phone-stack:has(.cl-seats, [data-setting-anchor='cloud.upgrade']) > .st-rc) {
		display: flex;
		flex-direction: column;
		align-items: stretch;
		/* With the button's own 6px tap margin, 12px between meter and button. */
		gap: 6px;
	}
	:global(.settings-shell[data-stacked]) [data-setting-anchor='cloud.upgrade'] { width: 100%; justify-content: center; }
	/* Phone: the account status sits on the title line, not between title and hint. */
	:global(.settings-shell[data-stacked] [data-setting-anchor='cloud.account']:not(.st-phone-stack) > .st-rc) { align-self: flex-start; }
	:global(.settings-shell[data-stacked]) [data-testid='cloud-state'] { display: flex; align-items: center; min-height: 21px; }
	:global(.settings-shell[data-stacked] [data-setting-anchor='cloud.account']:has(.st-rl .st-help)) [data-testid='cloud-state'] { min-height: 44px; }
	.cl-foot { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
	.cl-msg { margin: 0; min-width: 0; }
	@media (max-width: 600px) { .cl-foot { flex-direction: column; align-items: flex-start; gap: 6px; } }
</style>
