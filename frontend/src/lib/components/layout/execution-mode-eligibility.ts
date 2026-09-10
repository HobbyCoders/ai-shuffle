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

import type { ClusterGitState, Install } from '$lib/api/installs';

/** Per-install checkout eligibility. `null` = unknown (fail-open). */
export type EligibilityMap = Record<string, boolean> | null;

/** Only a successful worker read can establish that a checkout is missing. */
export function checkoutEligibility(state: ClusterGitState | null): EligibilityMap {
	if (!state?.project_v2_id) return null;
	const map: Record<string, boolean> = {};
	for (const row of state.installs) {
		// Older servers report false for offline workers without an error field.
		if ((row.is_self || row.online) && !row.availability_error) {
			map[row.id] = row.eligible;
		}
	}
	return map;
}

/** Other network workers, including offline. "This device" is Local, not a Cloud row. */
export function peerRunners(installs: Install[]): Install[] {
	return installs.filter(i => !i.is_self && i.approved !== false);
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
	return install.approved === false || !install.online || (worktreeOn && isIneligible(eligibility, install.id));
}

/** Tooltip for a Cloud accordion row. Offline wins over the checkout message. */
export function runnerTitle(
	install: Install,
	worktreeOn: boolean,
	eligibility: EligibilityMap
): string {
	if (install.approved === false) return `${install.device_name} is waiting for admin approval`;
	if (!install.online) return `${install.device_name} is offline`;
	if (isIneligible(eligibility, install.id)) {
		return worktreeOn
			? `No checkout of this project on ${install.device_name} — a worktree can't be created there`
			: `No checkout of this project on ${install.device_name}`;
	}
	return `Run this session on ${install.device_name}`;
}

/** Empty state: shown only when there are no peer runners at all. */
export const emptyRunnersCopy = 'No other workers in this network';
