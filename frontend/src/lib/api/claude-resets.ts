/**
 * Claude subscription resets: weekly (grants of resets that clear the weekly
 * limits) and the 5-hour reset. Mirrors the `/api/v1/usage/claude/resets`
 * routes in `app/api/usage.py`. Owner only.
 *
 * The worker reads the Claude Code CLI's current sign-in for each call and
 * never refreshes it; an expired sign-in answers 409 with a message to run a
 * Claude turn first.
 */

import { api } from './client';

export interface ClaudeWeeklyGrant {
	id: string;
	label: string;
	resets_total: number;
	resets_left: number;
	starts_at: string | null;
	ends_at: string | null;
	/** Window types a reset from this grant clears. */
	clears: string[];
	paused: boolean;
	usable_now: boolean;
	/** The grant can only be used while a weekly limit is hit. */
	use_requires_limit: boolean;
	blocking: string[];
}

export interface ClaudeWeeklyResets {
	eligible: boolean;
	ineligible_reason: string | null;
	at_limit: boolean;
	exhausted: string[];
	grants: ClaudeWeeklyGrant[];
	next_grant_id: string | null;
	weekly_resets_at: string | null;
	cooldown_until: string | null;
}

export interface ClaudeSessionReset {
	eligible: boolean;
	ineligible_reason: string | null;
	available: boolean;
	next_available_at: string | null;
	weekly_resets_at: string | null;
	resets_per_week: number;
}

export interface ClaudeResetsResponse {
	/** Null when the account has no weekly reset program. */
	weekly: ClaudeWeeklyResets | null;
	/** Null when the account has no 5-hour reset program. */
	session: ClaudeSessionReset | null;
}

export interface ClaudeWeeklyResetResponse {
	result: 'reset' | 'already_used' | 'not_limited' | 'cooldown' | 'ineligible' | 'unavailable';
	reason: string | null;
	resets_left: number | null;
	cleared: string[];
	weekly_resets_at: string | null;
	cooldown_until: string | null;
}

export interface ClaudeSessionResetResponse {
	result: 'reset' | 'already_used' | 'not_limited' | 'ineligible' | 'unavailable';
	next_available_at: string | null;
}

export function getClaudeResets(): Promise<ClaudeResetsResponse> {
	return api.get('/usage/claude/resets');
}

/** Spend one weekly reset. Reuse `idempotency_key` when retrying a failed call. */
export function claimClaudeWeeklyReset(body: {
	grant_id: string;
	idempotency_key: string;
}): Promise<ClaudeWeeklyResetResponse> {
	return api.post('/usage/claude/resets/weekly', body);
}

/** Use the 5-hour reset. A repeat answers `already_used`. */
export function claimClaudeSessionReset(): Promise<ClaudeSessionResetResponse> {
	return api.post('/usage/claude/resets/session', {});
}
