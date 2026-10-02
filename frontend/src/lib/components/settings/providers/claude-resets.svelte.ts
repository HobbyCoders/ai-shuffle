/**
 * Claude subscription resets (Claude > Usage): the weekly grants and the
 * 5-hour reset, each spent behind an inline confirm.
 *
 * A weekly claim carries an idempotency key that is REUSED on retry, so an
 * ambiguous failure can never spend a second reset; cancelling forgets it.
 * The 5-hour claim needs no key: a repeat answers "already used". After any
 * claim the status and the usage meters are read again.
 */
import { getErrorMessage } from '$lib/api/client';
import {
	claimClaudeSessionReset,
	claimClaudeWeeklyReset,
	getClaudeResets,
	type ClaudeResetsResponse,
	type ClaudeSessionReset,
	type ClaudeSessionResetResponse,
	type ClaudeWeeklyGrant,
	type ClaudeWeeklyResetResponse,
	type ClaudeWeeklyResets
} from '$lib/api/claude-resets';
import { providers } from '$lib/stores/providers.svelte';

/** The armed or running claim: a weekly grant id, or the 5-hour reset. */
export const SESSION_RESET = 'five_hour';

const dayFormatter = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });
const timeFormatter = new Intl.DateTimeFormat(undefined, {
	month: 'short',
	day: 'numeric',
	hour: 'numeric',
	minute: '2-digit'
});

function parse(iso: string | null | undefined): Date | null {
	if (!iso) return null;
	const date = new Date(iso);
	return Number.isNaN(date.getTime()) ? null : date;
}

/** "Oct 30" for an ISO date, or null. */
export function formatDay(iso: string | null | undefined): string | null {
	const date = parse(iso);
	return date ? dayFormatter.format(date) : null;
}

/** "Oct 9, 3:00 PM" for an ISO time, or null. */
export function formatTime(iso: string | null | undefined): string | null {
	const date = parse(iso);
	return date ? timeFormatter.format(date) : null;
}

const WEEKLY_INELIGIBLE: Record<string, string> = {
	tier: 'Not on this plan',
	seat: 'Not on this seat',
	cli_version: 'Update Claude Code',
	no_grant: 'No resets granted',
	tenure: 'Not offered yet'
};

const SESSION_INELIGIBLE: Record<string, string> = {
	tier: 'Not on this plan',
	cli_version: 'Update Claude Code',
	not_at_wall: 'Opens at the 5-hour limit',
	weekly_limit: 'Weekly limit hit',
	no_weekly_limit: 'Not on this plan',
	extra_usage: 'Off with extra usage',
	tenure: 'Not offered yet'
};

export function weeklyOutcome(result: ClaudeWeeklyResetResponse): string {
	switch (result.result) {
		case 'reset':
			return result.resets_left === null
				? 'Weekly limit reset.'
				: `Weekly limit reset. ${result.resets_left} ${result.resets_left === 1 ? 'reset' : 'resets'} left.`;
		case 'already_used':
			return 'That reset was already used.';
		case 'not_limited':
			return 'Nothing to reset yet: you are under the weekly limit.';
		case 'cooldown': {
			const until = formatTime(result.cooldown_until);
			return until ? `Resets are cooling down until ${until}.` : 'Resets are cooling down. Try again later.';
		}
		case 'ineligible':
			return 'This account can’t use weekly resets right now.';
		default:
			return 'Claude couldn’t reset right now. Try again later.';
	}
}

export function sessionOutcome(result: ClaudeSessionResetResponse): string {
	const next = formatTime(result.next_available_at);
	switch (result.result) {
		case 'reset':
			return '5-hour limit reset.';
		case 'already_used':
			return next ? `The 5-hour reset is used until ${next}.` : 'The 5-hour reset is already used.';
		case 'not_limited':
			return 'Nothing to reset yet: you are under the 5-hour limit.';
		case 'ineligible':
			return 'This account can’t use the 5-hour reset right now.';
		default:
			return 'Claude couldn’t reset right now. Try again later.';
	}
}

export class ClaudeResets {
	status = $state<ClaudeResetsResponse | null>(null);
	hasLoaded = $state(false);
	loading = $state(false);
	loadError = $state<string | null>(null);
	claimError = $state<string | null>(null);
	outcomeMessage = $state<string | null>(null);
	armed = $state<string | null>(null);
	claiming = $state<string | null>(null);
	liveMessage = $state('');

	#pending = $state<{ grantId: string; key: string } | null>(null);

	get weekly(): ClaudeWeeklyResets | null {
		return this.status?.weekly ?? null;
	}

	get session(): ClaudeSessionReset | null {
		return this.status?.session ?? null;
	}

	get busy(): boolean {
		return this.claiming !== null;
	}

	/** Weekly resets usable right now, across grants. */
	get weeklyLeft(): number {
		return (this.weekly?.grants ?? []).filter((g) => this.canUseGrant(g)).reduce((n, g) => n + g.resets_left, 0);
	}

	canUseGrant(grant: ClaudeWeeklyGrant): boolean {
		const weekly = this.weekly;
		return !!weekly?.eligible && grant.usable_now && !grant.paused && grant.resets_left > 0;
	}

	grantHint(grant: ClaudeWeeklyGrant): string {
		if (grant.resets_left === 0) return 'All used';
		if (grant.paused) return 'Paused';
		const cooldown = formatTime(this.weekly?.cooldown_until);
		if (cooldown) return `Cooling down until ${cooldown}`;
		if (!grant.usable_now && grant.use_requires_limit && !this.weekly?.at_limit) return 'Opens at the weekly limit';
		const by = formatDay(grant.ends_at);
		const left = `${grant.resets_left} of ${grant.resets_total || grant.resets_left} left`;
		return by ? `${left}, by ${by}` : left;
	}

	weeklyIneligible(): string {
		const reason = this.weekly?.ineligible_reason ?? '';
		return WEEKLY_INELIGIBLE[reason] ?? 'Not on this account';
	}

	canUseSession(): boolean {
		const s = this.session;
		return !!s?.eligible && s.available;
	}

	sessionHint(): string {
		const s = this.session;
		if (!s) return '';
		if (!s.eligible) return SESSION_INELIGIBLE[s.ineligible_reason ?? ''] ?? 'Not on this account';
		if (!s.available) {
			const next = formatTime(s.next_available_at);
			return next ? `Used, back ${next}` : 'Used this week';
		}
		return s.resets_per_week === 1 ? 'Once a week' : `${s.resets_per_week} a week`;
	}

	retryPending(grantId: string): boolean {
		return this.#pending?.grantId === grantId && this.claiming !== grantId;
	}

	async load(): Promise<void> {
		if (this.busy || this.loading) return;
		this.loading = true;
		this.loadError = null;
		try {
			this.status = await getClaudeResets();
			this.hasLoaded = true;
		} catch (error) {
			this.loadError = getErrorMessage(error);
			this.liveMessage = `Claude resets could not be loaded. ${this.loadError}`;
		} finally {
			this.loading = false;
		}
	}

	/** Read once per open detail pane; Refresh reads again. */
	loadOnce(): void {
		if (!this.hasLoaded && !this.loadError) void this.load();
	}

	arm(id: string): void {
		if (this.busy) return;
		if (this.#pending?.grantId !== id) this.#pending = null;
		this.armed = id;
		this.claimError = null;
	}

	cancel(id: string): void {
		if (this.busy) return;
		if (this.armed === id) this.armed = null;
		if (this.#pending?.grantId === id) this.#pending = null;
		this.claimError = null;
	}

	async useWeekly(grantId: string): Promise<void> {
		if (this.busy) return;
		const attempt = this.#pending?.grantId === grantId ? this.#pending : { grantId, key: crypto.randomUUID() };
		this.#pending = attempt;
		await this.#claim(grantId, 'Using weekly reset…', async () => {
			const result = await claimClaudeWeeklyReset({ grant_id: grantId, idempotency_key: attempt.key });
			this.#pending = null;
			return weeklyOutcome(result);
		});
	}

	async useSession(): Promise<void> {
		if (this.busy) return;
		await this.#claim(SESSION_RESET, 'Using 5-hour reset…', async () =>
			sessionOutcome(await claimClaudeSessionReset())
		);
	}

	async #claim(id: string, progress: string, run: () => Promise<string>): Promise<void> {
		this.claiming = id;
		this.claimError = null;
		this.outcomeMessage = null;
		this.liveMessage = progress;
		try {
			this.outcomeMessage = await run();
			this.liveMessage = this.outcomeMessage;
			this.armed = null;
		} catch (error) {
			this.claimError = getErrorMessage(error);
			this.liveMessage = `The reset could not be used. ${this.claimError}`;
			return;
		} finally {
			this.claiming = null;
		}
		await Promise.all([this.load(), providers.loadUsage()]);
	}
}
