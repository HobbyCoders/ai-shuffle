/**
 * The subscription loop guard: a budget for unattended Claude turns
 * (automations, wake-ups, background workers, connectors) while Claude Code
 * runs on the user's subscription, a default time limit for background
 * workers, and the turns it holds for the owner's confirmation.
 */

import { api } from './client';

export interface LoopGuardSettings {
	enabled: boolean;
	session_turns_per_hour: number;
	install_turns_per_hour: number;
	worker_max_minutes: number;
}

export interface HeldTurn {
	id: string;
	session_id: string;
	turn_id: string | null;
	source: string;
	prompt: string | null;
	reason: string;
	/** Unix seconds. */
	held_at: number;
}

export interface LoopGuardStatus extends LoopGuardSettings {
	defaults: LoopGuardSettings;
	/** Enabled and Claude Code is in subscription mode. */
	applies: boolean;
	unattended_sources: string[];
	usage: { install_last_hour: number };
	held: HeldTurn[];
}

export function getLoopGuard(): Promise<LoopGuardStatus> {
	return api.get('/settings/loop-guard', { cache: 'no-store' });
}

export function updateLoopGuard(patch: Partial<LoopGuardSettings>): Promise<LoopGuardStatus> {
	return api.put('/settings/loop-guard', patch);
}

export function confirmHeldTurn(id: string): Promise<{ success: boolean; turn_id: string }> {
	return api.post(`/settings/loop-guard/held/${encodeURIComponent(id)}/confirm`, {});
}

export function dismissHeldTurn(id: string): Promise<{ success: boolean }> {
	return api.post(`/settings/loop-guard/held/${encodeURIComponent(id)}/dismiss`, {});
}
