/**
 * Menu items for the context-bar runner badge.
 *
 * The worker the session runs on comes first as the checked row (no action),
 * like Local in the new-chat picker. Continue here → POST /adopt.
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
	/** Board-row name and presence of a remote owner, used when the registry has no row for it. */
	ownerName?: string | null;
	ownerOnline?: boolean | null;
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
	/** The worker this session runs on now. Activating it does nothing. */
	checked: boolean;
}

/** Id of the checked row for the worker the session already runs on. */
export const CURRENT_RUNNER_ID = 'current-runner';

/** The tag a worker row carries in either runner picker. */
export function workerRowNote(install: Install, eligibility: EligibilityMap): string | null {
	if (!install.online) return 'offline';
	if (isIneligible(eligibility, install.id)) return 'no checkout';
	return null;
}

function currentRunnerItem(input: RunnerBadgeMenuInput): RunnerBadgeMenuItem {
	if (!input.isRemote) {
		// This worker, named like the runner chip and the new-chat picker.
		return {
			id: CURRENT_RUNNER_ID, label: 'Local', action: null, disabled: false, online: true,
			note: null, checked: true, title: 'This session runs on this device'
		};
	}
	const owner = input.installs.find((i) => i.id === input.ownerId);
	const name = owner?.device_name || input.ownerName || 'Another worker';
	const online = owner ? owner.online : input.ownerOnline !== false;
	return {
		id: CURRENT_RUNNER_ID,
		label: name,
		action: null,
		disabled: false,
		online,
		note: online ? null : 'offline',
		checked: true,
		title: online ? `This session runs on ${name}` : `This session runs on ${name}, which is offline`
	};
}

export function runnerBadgeMenuItems(input: RunnerBadgeMenuInput): RunnerBadgeMenuItem[] {
	const items: RunnerBadgeMenuItem[] = [currentRunnerItem(input)];
	if (input.isRemote) items.push(
		{
			id: 'continue-here',
			label: 'Continue here',
			action: { kind: 'adopt' },
			disabled: false,
			online: true,
			note: null,
			checked: false,
			title: input.hasProject
				? 'Commit and push on the owning worker, then fetch the branch and sync context here'
				: 'Move this chat and its history to this runner'
		}
	);
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
			checked: false,
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
