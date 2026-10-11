/**
 * Menu items for the context-bar runner badge.
 *
 * Continue here → POST /adopt.
 * Other runners → /move. Offline / ineligible rows are listed and greyed
 * via execution-mode-eligibility (same helpers as the Cloud accordion).
 */

import type { Install } from '$lib/api/installs';
import {
	peerRunners,
	isBlocked,
	isIneligible,
	runnerTitle,
	type EligibilityMap
} from '$lib/components/layout/execution-mode-eligibility';

export interface RunnerBadgeMenuInput {
	ownerId: string | null | undefined;
	/** Only remote sessions can be continued on this worker. */
	isRemote: boolean;
	installs: Install[];
	eligibility: EligibilityMap;
	/** Session has a project — ineligible runners 409 on /move. */
	hasProject: boolean;
	/** Session is actually a worktree — only then does the tooltip mention one. */
	isWorktree: boolean;
}

export type RunnerBadgeMenuAction =
	| { kind: 'adopt' }
	| { kind: 'move'; targetInstanceId: string };

export interface RunnerBadgeMenuItem {
	id: string;
	label: string;
	action: RunnerBadgeMenuAction | null;
	disabled: boolean;
	title: string;
	/** Drives the presence dot. "Continue here" is this worker, always online. */
	online: boolean;
	/** Trailing tag shared with the new-chat picker: "offline" or "no checkout". */
	note: string | null;
}

/** The tag a worker row carries in either runner picker. */
export function workerRowNote(install: Install, eligibility: EligibilityMap): string | null {
	if (!install.online) return 'offline';
	if (isIneligible(eligibility, install.id)) return 'no checkout';
	return null;
}

export function runnerBadgeMenuItems(input: RunnerBadgeMenuInput): RunnerBadgeMenuItem[] {
	const items: RunnerBadgeMenuItem[] = input.isRemote ? [
		{
			id: 'continue-here',
			label: 'Continue here',
			action: { kind: 'adopt' },
			disabled: false,
			online: true,
			note: null,
			title: input.hasProject
				? 'Commit and push on the owning worker, then fetch the branch and sync context here'
				: 'Move this chat and its history to this runner'
		}
	] : [];
	const peers = peerRunners(input.installs).filter((i) => i.id !== input.ownerId);
	for (const install of peers) {
		const blocked = isBlocked(install, input.hasProject, input.eligibility);
		items.push({
			id: `move-${install.id}`,
			label: install.device_name,
			action: blocked ? null : { kind: 'move', targetInstanceId: install.id },
			disabled: blocked,
			online: install.online,
			note: workerRowNote(install, input.eligibility),
			title: runnerTitle(install, input.isWorktree, input.eligibility)
		});
	}
	return items;
}

/** Query the session's canonical binding; fall back to a legacy-only project. */
export function gitStateQuery(session: {
	project_id?: string | null;
	project_v2_id?: string | null;
}): { project_id: string } | { project_v2_id: string } | null {
	if (session.project_v2_id) return { project_v2_id: session.project_v2_id };
	if (session.project_id) return { project_id: session.project_id };
	return null;
}
