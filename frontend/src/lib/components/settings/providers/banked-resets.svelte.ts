/**
 * Banked resets of a ChatGPT (Codex) or SuperGrok subscription: the inventory
 * and the one-shot "Use" that spends a reset.
 *
 * A credit is armed before it is used, and consumed under an idempotency key
 * that is REUSED on retry, so an ambiguous failure can never spend a second
 * credit. A 409 re-reads the providers store so the row's credential state
 * catches up. Cancelling an armed credit forgets its key.
 */
import { getErrorMessage, isApiError } from '$lib/api/client';
import {
	consumeCodexRateLimitResetCredit,
	consumeGrokRateLimitResetCredit,
	listCodexRateLimitResetCredits,
	listGrokRateLimitResetCredits,
	type CodexRateLimitResetCredit,
	type ConsumeCodexRateLimitResetCreditResponse,
	type ModelProvider
} from '$lib/api/model-providers';
import { providers } from '$lib/stores/providers.svelte';

export const GROK_DEFAULT_DESCRIPTION =
	"A reset clears your weekly usage once. Resets don't stack and expire on their date.";

export function isGrokSubscription(provider: Pick<ModelProvider, 'provider_type' | 'auth_mode'>): boolean {
	return provider.provider_type === 'grok' || provider.auth_mode === 'grok';
}

const dateFormatter = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium' });

/** "Oct 14, 2026" from unix seconds, or null when there is no usable date. */
export function formatResetDate(value: number | null): string | null {
	if (value === null || !Number.isFinite(value) || value <= 0) return null;
	const date = new Date(value * 1000);
	if (Number.isNaN(date.getTime())) return null;
	return dateFormatter.format(date);
}

export class BankedResets {
	credits = $state<CodexRateLimitResetCredit[]>([]);
	availableCount = $state(0);
	hasLoaded = $state(false);
	loading = $state(false);
	refreshing = $state(false);
	inventoryError = $state<string | null>(null);
	consumeError = $state<string | null>(null);
	armedCreditId = $state<string | null>(null);
	consumingCreditId = $state<string | null>(null);
	outcomeMessage = $state<string | null>(null);
	liveMessage = $state('Loading banked resets…');

	#pendingAttempt = $state<{ creditId: string; idempotencyKey: string } | null>(null);
	#provider: () => ModelProvider;

	constructor(provider: () => ModelProvider) {
		this.#provider = provider;
	}

	get isGrok(): boolean {
		return isGrokSubscription(this.#provider());
	}

	/** A failed Use of this credit is waiting to be retried with the same key. */
	retryPending(creditId: string): boolean {
		return this.#pendingAttempt?.creditId === creditId && this.consumingCreditId !== creditId;
	}

	get consumeActive(): boolean {
		return this.consumingCreditId !== null;
	}

	/** Credits the inventory lists as available (may be fewer than `availableCount`). */
	get detailedAvailableCount(): number {
		return this.credits.filter((c) => c.status === 'available').length;
	}

	title(credit: CodexRateLimitResetCredit): string {
		return credit.title?.trim() || (this.isGrok ? 'Usage limit reset' : 'Codex rate-limit reset');
	}

	description(credit: CodexRateLimitResetCredit): string | null {
		if (credit.description?.trim()) return credit.description;
		return this.isGrok ? GROK_DEFAULT_DESCRIPTION : null;
	}

	statusLabel(credit: CodexRateLimitResetCredit): string {
		if (credit.status === 'available') return 'Available';
		if (credit.status === 'redeeming') return 'Being used';
		if (credit.status === 'redeemed') return 'Used';
		return 'Status unavailable';
	}

	canUse(credit: CodexRateLimitResetCredit): boolean {
		if (credit.status !== 'available') return false;
		return credit.reset_type === (this.isGrok ? 'grok_rate_limits' : 'codex_rate_limits');
	}

	async refresh(): Promise<void> {
		if (this.consumeActive) return;
		if (this.hasLoaded) this.refreshing = true;
		else this.loading = true;
		this.inventoryError = null;
		this.liveMessage = this.hasLoaded ? 'Refreshing banked resets…' : 'Loading banked resets…';
		try {
			const id = this.#provider().id;
			const result = this.isGrok ? await listGrokRateLimitResetCredits(id) : await listCodexRateLimitResetCredits(id);
			this.credits = result.credits;
			this.availableCount = result.available_count;
			this.hasLoaded = true;
			this.liveMessage = `${result.available_count} banked ${result.available_count === 1 ? 'reset' : 'resets'} available.`;
		} catch (error) {
			this.inventoryError = getErrorMessage(error);
			this.liveMessage = `Banked resets could not be loaded. ${this.inventoryError}`;
		} finally {
			this.loading = false;
			this.refreshing = false;
		}
	}

	arm(creditId: string): void {
		if (this.consumeActive) return;
		if (this.#pendingAttempt?.creditId !== creditId) this.#pendingAttempt = null;
		this.armedCreditId = creditId;
		this.consumeError = null;
	}

	cancel(creditId: string): void {
		if (this.consumeActive) return;
		if (this.armedCreditId === creditId) this.armedCreditId = null;
		if (this.#pendingAttempt?.creditId === creditId) this.#pendingAttempt = null;
		this.consumeError = null;
	}

	outcome(result: ConsumeCodexRateLimitResetCreditResponse): string {
		const grok = this.isGrok;
		if (result.outcome === 'reset') {
			const one = result.windows_reset === 1;
			return grok
				? `${result.windows_reset} eligible Grok weekly usage ${one ? 'window was' : 'windows were'} reset.`
				: `${result.windows_reset} eligible Codex ${one ? 'window was' : 'windows were'} reset.`;
		}
		if (result.outcome === 'nothing_to_reset') {
			return grok ? 'No eligible Grok weekly usage needed a reset.' : 'No eligible Codex windows needed a reset.';
		}
		if (result.outcome === 'no_credit') return 'This banked reset is no longer available.';
		return 'This banked reset was already used.';
	}

	/** Spend a credit. The idempotency key of a failed attempt is reused. */
	async consume(creditId: string): Promise<void> {
		if (this.consumeActive) return;
		const attempt =
			this.#pendingAttempt?.creditId === creditId
				? this.#pendingAttempt
				: { creditId, idempotencyKey: crypto.randomUUID() };
		this.#pendingAttempt = attempt;
		this.consumingCreditId = creditId;
		this.consumeError = null;
		this.outcomeMessage = null;
		this.liveMessage = 'Using banked reset…';
		try {
			const id = this.#provider().id;
			const body = { credit_id: creditId, idempotency_key: attempt.idempotencyKey };
			const result = this.isGrok
				? await consumeGrokRateLimitResetCredit(id, body)
				: await consumeCodexRateLimitResetCredit(id, body);
			this.outcomeMessage = this.outcome(result);
			this.liveMessage = this.outcomeMessage;
			this.armedCreditId = null;
			this.#pendingAttempt = null;
			this.consumingCreditId = null;
			await this.refresh();
		} catch (error) {
			this.consumeError = getErrorMessage(error);
			this.liveMessage = `The banked reset could not be used. ${this.consumeError}`;
			if (isApiError(error) && error.status === 409) {
				try {
					await providers.refreshProviders();
				} catch {
					// The consume error stays the actionable message; the key stays ready.
				}
			}
		} finally {
			this.consumingCreditId = null;
		}
	}
}
