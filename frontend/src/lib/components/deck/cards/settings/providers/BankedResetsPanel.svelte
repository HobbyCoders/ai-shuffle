<script lang="ts">
	/**
	 * BankedResetsPanel — the ChatGPT / Grok reset inventory, inline in the row's
	 * panel (it used to be a modal dialog).
	 *
	 * Mechanics are unchanged from the dialog: load on mount, refresh on demand,
	 * arm a credit before using it, consume it under an idempotency key that is
	 * REUSED on retry (an ambiguous failure must not spend a second credit), and
	 * re-read the providers store on a 409 so the row's credential state catches
	 * up. Only the chrome is gone.
	 */
	import { onMount } from 'svelte';
	import { RefreshCw } from 'lucide-svelte';
	import {
		consumeCodexRateLimitResetCredit,
		consumeGrokRateLimitResetCredit,
		listCodexRateLimitResetCredits,
		listGrokRateLimitResetCredits,
		type CodexRateLimitResetCredit,
		type ConsumeCodexRateLimitResetCreditResponse,
		type ModelProvider
	} from '$lib/api/model-providers';
	import { getErrorMessage, isApiError } from '$lib/api/client';
	import { providers } from '$lib/stores/providers.svelte';

	interface Props {
		provider: ModelProvider;
	}
	let { provider }: Props = $props();

	const GROK_DEFAULT_DESCRIPTION =
		"A reset clears your weekly usage once. Resets don't stack and expire on their date.";

	let isGrok = $derived(provider.provider_type === 'grok' || provider.auth_mode === 'grok');

	let credits = $state<CodexRateLimitResetCredit[]>([]);
	let availableCount = $state(0);
	let hasLoaded = $state(false);
	let loading = $state(true);
	let refreshing = $state(false);
	let inventoryError = $state<string | null>(null);
	let consumeError = $state<string | null>(null);
	let armedCreditId = $state<string | null>(null);
	let consumingCreditId = $state<string | null>(null);
	let pendingAttempt = $state<{ creditId: string; idempotencyKey: string } | null>(null);
	let outcomeMessage = $state<string | null>(null);
	let liveMessage = $state('Loading banked resets…');

	let headerLabel = $derived(
		hasLoaded ? `Banked resets · ${availableCount} available` : 'Banked resets'
	);
	let consumeActive = $derived(consumingCreditId !== null);
	let detailedAvailableCount = $derived(
		credits.filter((credit) => credit.status === 'available').length
	);

	const dateFormatter = new Intl.DateTimeFormat(undefined, {
		dateStyle: 'medium',
		timeStyle: 'short'
	});

	onMount(() => {
		void refreshInventory();
	});

	function formatUnixSeconds(value: number | null): string | null {
		if (value === null || !Number.isFinite(value) || value <= 0) return null;
		const date = new Date(value * 1000);
		if (Number.isNaN(date.getTime())) return null;
		return dateFormatter.format(date);
	}

	function creditTitle(credit: CodexRateLimitResetCredit): string {
		return credit.title?.trim() || (isGrok ? 'Usage limit reset' : 'Codex rate-limit reset');
	}

	function creditDescription(credit: CodexRateLimitResetCredit): string | null {
		if (credit.description?.trim()) return credit.description;
		return isGrok ? GROK_DEFAULT_DESCRIPTION : null;
	}

	function statusLabel(credit: CodexRateLimitResetCredit): string {
		if (credit.status === 'available') return 'Available';
		if (credit.status === 'redeeming') return 'Being used';
		if (credit.status === 'redeemed') return 'Used';
		return 'Status unavailable';
	}

	function canUse(credit: CodexRateLimitResetCredit): boolean {
		if (credit.status !== 'available') return false;
		if (isGrok) return credit.reset_type === 'grok_rate_limits';
		return credit.reset_type === 'codex_rate_limits';
	}

	async function listCredits() {
		if (isGrok) return listGrokRateLimitResetCredits(provider.id);
		return listCodexRateLimitResetCredits(provider.id);
	}

	async function consumeCreditRequest(
		creditId: string,
		idempotencyKey: string
	): Promise<ConsumeCodexRateLimitResetCreditResponse> {
		const body = { credit_id: creditId, idempotency_key: idempotencyKey };
		if (isGrok) return consumeGrokRateLimitResetCredit(provider.id, body);
		return consumeCodexRateLimitResetCredit(provider.id, body);
	}

	async function refreshInventory(): Promise<void> {
		if (consumeActive) return;
		if (hasLoaded) refreshing = true;
		else loading = true;
		inventoryError = null;
		liveMessage = hasLoaded ? 'Refreshing banked resets…' : 'Loading banked resets…';

		try {
			const result = await listCredits();
			credits = result.credits;
			availableCount = result.available_count;
			hasLoaded = true;
			liveMessage = `${result.available_count} banked ${result.available_count === 1 ? 'reset' : 'resets'} available.`;
		} catch (error) {
			inventoryError = getErrorMessage(error);
			liveMessage = `Banked resets could not be loaded. ${inventoryError}`;
		} finally {
			loading = false;
			refreshing = false;
		}
	}

	function armCredit(creditId: string): void {
		if (consumeActive) return;
		if (pendingAttempt?.creditId !== creditId) pendingAttempt = null;
		armedCreditId = creditId;
		consumeError = null;
	}

	function cancelCredit(creditId: string): void {
		if (consumeActive) return;
		if (armedCreditId === creditId) armedCreditId = null;
		if (pendingAttempt?.creditId === creditId) pendingAttempt = null;
		consumeError = null;
	}

	function messageForOutcome(result: ConsumeCodexRateLimitResetCreditResponse): string {
		if (result.outcome === 'reset') {
			if (isGrok) {
				return `${result.windows_reset} eligible Grok weekly usage ${result.windows_reset === 1 ? 'window was' : 'windows were'} reset.`;
			}
			return `${result.windows_reset} eligible Codex ${result.windows_reset === 1 ? 'window was' : 'windows were'} reset.`;
		}
		if (result.outcome === 'nothing_to_reset') {
			return isGrok
				? 'No eligible Grok weekly usage needed a reset.'
				: 'No eligible Codex windows needed a reset.';
		}
		if (result.outcome === 'no_credit') {
			return 'This banked reset is no longer available.';
		}
		return 'This banked reset was already used.';
	}

	async function consumeCredit(creditId: string): Promise<void> {
		if (consumeActive) return;

		// The same key is reused on retry: an ambiguous failure must never be able
		// to spend a second credit.
		const attempt =
			pendingAttempt?.creditId === creditId
				? pendingAttempt
				: { creditId, idempotencyKey: crypto.randomUUID() };
		pendingAttempt = attempt;
		consumingCreditId = creditId;
		consumeError = null;
		outcomeMessage = null;
		liveMessage = 'Using banked reset…';

		try {
			const result = await consumeCreditRequest(creditId, attempt.idempotencyKey);
			outcomeMessage = messageForOutcome(result);
			liveMessage = outcomeMessage;
			armedCreditId = null;
			pendingAttempt = null;
			consumingCreditId = null;
			await refreshInventory();
		} catch (error) {
			consumeError = getErrorMessage(error);
			liveMessage = `The banked reset could not be used. ${consumeError}`;
			if (isApiError(error) && error.status === 409) {
				try {
					await providers.refreshProviders();
				} catch {
					// Keep the consume error as the actionable message if the provider
					// refresh also fails; the same key stays ready for retry.
				}
			}
		} finally {
			consumingCreditId = null;
		}
	}
</script>

<div class="br-head">
	<span class="br-title-lab">{headerLabel}</span>
	<button
		type="button"
		class="br-iconbtn"
		onclick={refreshInventory}
		disabled={consumeActive || loading || refreshing}
		aria-label="Refresh"
		title="Refresh"
	>
		<RefreshCw />
	</button>
</div>

<p class="sr-only" aria-live="polite">{liveMessage}</p>

{#if inventoryError}
	<p class="br-err" role="alert">{inventoryError}</p>
	<button
		type="button"
		class="ed-btn br-sm br-retry"
		onclick={refreshInventory}
		disabled={consumeActive || loading || refreshing}
	>
		Try again
	</button>
{/if}

{#if outcomeMessage}
	<p class="br-outcome" aria-live="polite">{outcomeMessage}</p>
{/if}

{#if loading && !hasLoaded}
	<p class="br-muted">Loading…</p>
{:else if hasLoaded && credits.length === 0}
	<p class="br-muted">No banked resets.</p>
{:else if hasLoaded}
	{#if availableCount > detailedAvailableCount}
		<p class="br-muted br-capped">
			{detailedAvailableCount} of {availableCount} available reset details are shown.
		</p>
	{/if}

	<ul class="br-list">
		{#each credits as credit (credit.id)}
			{@const granted = formatUnixSeconds(credit.granted_at)}
			{@const expires = formatUnixSeconds(credit.expires_at)}
			<li class="br-item">
				<div class="br-row">
					<div class="br-info">
						<span class="br-name">{creditTitle(credit)}</span>
						<span class="br-meta">
							<span>{expires ? `Expires ${expires}` : 'No expiration shown'}</span>
							<span class="br-sep" aria-hidden="true">·</span>
							<span>{statusLabel(credit)}</span>
							<span class="br-sep" aria-hidden="true">·</span>
							<span>{granted ? `Granted ${granted}` : 'Grant date unavailable'}</span>
						</span>
					</div>

					{#if canUse(credit)}
						<div class="br-act">
							{#if armedCreditId === credit.id}
								<button
									type="button"
									class="ed-btn primary br-sm"
									onclick={() => consumeCredit(credit.id)}
									disabled={consumeActive}
								>
									{consumingCreditId === credit.id
										? 'Using…'
										: consumeError
											? 'Try again'
											: 'Confirm'}
								</button>
								<button
									type="button"
									class="ed-btn br-sm"
									onclick={() => cancelCredit(credit.id)}
									disabled={consumeActive}
								>
									Cancel
								</button>
							{:else}
								<button
									type="button"
									class="ed-btn br-sm"
									onclick={() => armCredit(credit.id)}
									disabled={consumeActive}
								>
									Use
								</button>
							{/if}
						</div>
					{/if}
				</div>

				{#if creditDescription(credit)}
					<p class="br-desc">{creditDescription(credit)}</p>
				{/if}

				{#if armedCreditId === credit.id}
					<p class="br-prompt">Use this reset?</p>
					<p class="br-desc">
						{isGrok
							? 'Eligible Grok weekly usage may reset. This action cannot be undone.'
							: 'Eligible Codex windows may reset. This action cannot be undone.'}
					</p>
					{#if consumeError}
						<p class="br-err" role="alert">{consumeError}</p>
					{/if}
				{/if}
			</li>
		{/each}
	</ul>
{/if}

<style>
	.br-head {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 8px;
		margin-bottom: 8px;
	}
	.br-title-lab {
		font-size: 11px;
		font-weight: 600;
		letter-spacing: 0.03em;
		text-transform: uppercase;
		color: var(--muted-foreground);
	}
	.br-iconbtn {
		display: grid;
		place-items: center;
		width: 26px;
		height: 26px;
		flex-shrink: 0;
		border-radius: 7px;
		border: 1px solid var(--border);
		background: var(--background);
		color: var(--muted-foreground);
		cursor: pointer;
		transition: color 0.15s, border-color 0.15s;
	}
	.br-iconbtn:hover:not(:disabled) {
		color: var(--foreground);
		border-color: var(--foreground);
	}
	.br-iconbtn:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.br-iconbtn :global(svg) {
		width: 13px;
		height: 13px;
	}

	.br-list {
		list-style: none;
		margin: 0;
		padding: 0;
		display: flex;
		flex-direction: column;
		gap: 8px;
	}
	.br-item {
		min-width: 0;
	}
	.br-row {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: 10px;
	}
	.br-info {
		min-width: 0;
		display: flex;
		flex-direction: column;
		gap: 1px;
	}
	.br-name {
		font-size: 12px;
		font-weight: 600;
		color: var(--foreground);
	}
	.br-meta {
		display: flex;
		flex-wrap: wrap;
		gap: 5px;
		font-size: 11.5px;
		color: var(--muted-foreground);
	}
	.br-sep {
		opacity: 0.6;
	}
	.br-act {
		display: flex;
		align-items: center;
		gap: 6px;
		flex-shrink: 0;
	}
	.br-sm {
		display: inline-flex;
		align-items: center;
		padding: 4px 9px;
		font-size: 11.5px;
	}
	.br-retry {
		margin-top: 6px;
	}
	.br-desc,
	.br-prompt,
	.br-outcome {
		margin: 3px 0 0;
		font-size: 11.5px;
		line-height: 1.45;
	}
	.br-desc {
		color: var(--muted-foreground);
	}
	.br-prompt {
		color: var(--foreground);
		font-weight: 600;
	}
	.br-outcome {
		margin-bottom: 8px;
		color: var(--foreground);
	}
	.br-muted {
		margin: 0;
		font-size: 12px;
		color: var(--muted-foreground);
	}
	.br-capped {
		margin-bottom: 6px;
	}
	.br-err {
		margin: 3px 0 0;
		font-size: 11.5px;
		line-height: 1.45;
		color: var(--destructive);
	}
	.sr-only {
		position: absolute;
		width: 1px;
		height: 1px;
		padding: 0;
		margin: -1px;
		overflow: hidden;
		clip: rect(0, 0, 0, 0);
		white-space: nowrap;
		border: 0;
	}
</style>
