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
import type { ProjectOverviewRow, ProjectWorker } from '$lib/api/project-workers';
import { runsOnNote } from '$lib/components/deck/cards/project-workers-view';

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

// ─── New chat, once workers can set projects up for each other ──────────────
//
// With cross-worker project sync (GET /projects/overview answers), a worker
// that lacks the picked project stays selectable in the new-chat pickers,
// also with a worktree: "Set up & send" sets it up there first. Only an
// offline or unapproved worker is blocked. Without the overview (an older
// server) the rules above apply unchanged.

/** The overview row of the project a new chat has picked. */
export function overviewRowFor(
	overview: ProjectOverviewRow[],
	project: { project: string | null; projectV2Id: string | null }
): ProjectOverviewRow | undefined {
	if (!project.project && !project.projectV2Id) return undefined;
	return overview.find((r) =>
		(!!project.projectV2Id && r.project_v2_id === project.projectV2Id) ||
		(!!project.project && r.local_project_id === project.project));
}

/** The overview worker standing for an install (any of its ids), or this worker's row. */
export function overviewWorkerFor(row: ProjectOverviewRow | undefined, install: Install | null): ProjectWorker | undefined {
	if (!row) return undefined;
	if (!install) return row.workers.find((w) => w.is_self);
	const ids = [install.id, install.device_id, install.legacy_instance_id].filter(Boolean);
	return row.workers.find((w) => ids.includes(w.install_id)) ?? (install.is_self ? row.workers.find((w) => w.is_self) : undefined);
}

/** A new-chat picker row is blocked: with sync only offline/unapproved; else the classic rule. */
export function isBlockedForNewChat(
	install: Install,
	worktreeOn: boolean,
	eligibility: EligibilityMap,
	syncRow: ProjectOverviewRow | undefined
): boolean {
	if (syncRow) return install.approved === false || !install.online;
	return isBlocked(install, worktreeOn, eligibility);
}

/** The trailing note: "ready" / "not set up · set up" / "offline" with sync, else the classic note. */
export function newChatRowNote(
	install: Install,
	eligibility: EligibilityMap,
	syncRow: ProjectOverviewRow | undefined
): string | null {
	if (!install.online) return 'offline';
	if (syncRow) return runsOnNote(overviewWorkerFor(syncRow, install)) ?? null;
	if (isIneligible(eligibility, install.id)) return 'no checkout';
	return null;
}

/** The row's tooltip with sync: says a missing project is set up at send. */
export function newChatRowTitle(
	install: Install,
	worktreeOn: boolean,
	eligibility: EligibilityMap,
	syncRow: ProjectOverviewRow | undefined
): string {
	if (!syncRow || install.approved === false || !install.online) return runnerTitle(install, worktreeOn, eligibility);
	const worker = overviewWorkerFor(syncRow, install);
	if (worker && (worker.state === 'missing' || worker.state === 'failed')) {
		return `${syncRow.name} isn't on ${install.device_name} yet — it's set up there when you send`;
	}
	return `Run this session on ${install.device_name}`;
}

/** Empty state: shown only when there are no peer runners at all. */
export const emptyRunnersCopy = 'No other workers in this network';
