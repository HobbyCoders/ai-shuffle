/**
 * The owner's daily spending limit and job time limit, as Settings > Usage >
 * Spending shows them. The server enforces both and refuses to switch the
 * spending limit off without an explicit confirmation.
 */
import { getErrorMessage } from '$lib/api/client';
import { getSpendingLimits, updateSpendingLimits, type SpendingLimits, type SpendingLimitsPatch } from '$lib/api/spending-limits';
import { isForbidden } from '$lib/settings/access.svelte';
import { flashSaved } from '$lib/settings/save-status.svelte';

export const SPEND_BOUNDS = { min: 0.5, max: 10000 } as const;
const JOB_MINUTES = [30, 60, 120, 240, 480, 1440];

export function formatMoney(value: number): string {
	return new Intl.NumberFormat(undefined, { style: 'currency', currency: 'USD', maximumFractionDigits: 2 }).format(value);
}

export function formatJobTime(minutes: number): string {
	if (minutes % 60 !== 0) return `${minutes} minutes`;
	const hours = minutes / 60;
	return `${hours} ${hours === 1 ? 'hour' : 'hours'}`;
}

/** The offered job times, with a saved value outside the list kept in order. */
export function jobTimeOptions(current: number | null): number[] {
	return current !== null && !JOB_MINUTES.includes(current)
		? [...JOB_MINUTES, current].sort((a, b) => a - b)
		: JOB_MINUTES;
}

export class Spending {
	limits = $state<SpendingLimits | null>(null);
	loadError = $state('');
	forbidden = $state(false);
	saveError = $state('');
	busy = $state(false);

	/** Share of today's limit already spent, 0..1 (0 while the limit is off). */
	get usedRatio(): number {
		const l = this.limits;
		if (!l || !l.spend_limit_enabled || l.daily_spend_limit_usd <= 0) return 0;
		return Math.min(1, l.spent_today_usd / l.daily_spend_limit_usd);
	}

	async load(): Promise<void> {
		try {
			this.limits = await getSpendingLimits();
			this.loadError = '';
			this.forbidden = false;
		} catch (e) {
			if (isForbidden(e)) this.forbidden = true;
			else this.loadError = getErrorMessage(e, 'Could not read the spending limits.');
		}
	}

	/** Resolves false when the save failed (a Switch then flips back). */
	async save(patch: SpendingLimitsPatch): Promise<boolean> {
		if (this.busy) return false;
		this.busy = true;
		this.saveError = '';
		try {
			this.limits = await updateSpendingLimits(patch);
			flashSaved();
			return true;
		} catch (e) {
			this.saveError = getErrorMessage(e, 'Could not save the spending limits.');
			await this.load();
			return false;
		} finally {
			this.busy = false;
		}
	}
}
