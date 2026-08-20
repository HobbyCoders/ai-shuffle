/**
 * Composer Cloud-accordion eligibility helpers.
 *
 * Shared list/block/title contract for the accordion, runner badge, and
 * project picker. The Cloud list is `installs` (full processes that can
 * host a session), not `mobile_devices` (phones). Offline peers stay
 * listed and are greyed via `isBlocked`.
 *
 * W3-3 fail-open: a null eligibility map never adds extra disabling, so a
 * git-state hiccup cannot lock the user out of their runners.
 */

import type { Install } from '$lib/api/installs';

/** Per-install checkout eligibility. `null` = unknown (fail-open). */
export type EligibilityMap = Record<string, boolean> | null;

/** Other cluster installs, including offline. "This device" is Local, not a Cloud row. */
export function peerRunners(installs: Install[]): Install[] {
	return installs.filter(i => !i.is_self);
}

/**
 * True only when git-state explicitly reported this runner ineligible.
 * Missing keys and a null map fail open.
 */
export function isIneligible(eligibility: EligibilityMap, id: string): boolean {
	return eligibility !== null && eligibility[id] === false;
}

/**
 * Offline is always blocked. Worktree-on also blocks an ineligible runner
 * (a worktree needs a base checkout on the target). In plain mode the
 * "no checkout" badge is informational and the row stays selectable.
 */
export function isBlocked(
	install: Install,
	worktreeOn: boolean,
	eligibility: EligibilityMap
): boolean {
	return !install.online || (worktreeOn && isIneligible(eligibility, install.id));
}

/** Tooltip for a Cloud accordion row. Offline wins over the checkout message. */
export function runnerTitle(
	install: Install,
	worktreeOn: boolean,
	eligibility: EligibilityMap
): string {
	if (!install.online) return `${install.device_name} is offline`;
	if (isIneligible(eligibility, install.id)) {
		return worktreeOn
			? `No checkout of this project on ${install.device_name} — a worktree can't be created there`
			: `No checkout of this project on ${install.device_name}`;
	}
	return `Run this session on ${install.device_name}`;
}

/** Empty state: shown only when there are no peer runners at all. */
export const emptyRunnersCopy = 'No other runners in this cluster';
