/**
 * Menu items for the context-bar runner badge.
 *
 * Continue here → POST /adopt. Unpin (when pinned) → /adopt {follow:true}.
 * Other runners → /move. Offline / ineligible rows are listed and greyed
 * via execution-mode-eligibility (same helpers as the Cloud accordion).
 */

import type { Install } from '$lib/api/installs';
import {
	peerRunners,
	isBlocked,
	runnerTitle,
	type EligibilityMap
} from '$lib/components/layout/execution-mode-eligibility';

export interface RunnerBadgeMenuInput {
	ownerId: string | null | undefined;
	isPinned: boolean;
	installs: Install[];
	eligibility: EligibilityMap;
	/** Session has a project — ineligible runners 409 on /move. */
	hasProject: boolean;
}

export type RunnerBadgeMenuAction =
	| { kind: 'adopt' }
	| { kind: 'unpin' }
	| { kind: 'move'; targetInstanceId: string };

export interface RunnerBadgeMenuItem {
	id: string;
	label: string;
	action: RunnerBadgeMenuAction | null;
	disabled: boolean;
	title: string;
}

export function runnerBadgeMenuItems(input: RunnerBadgeMenuInput): RunnerBadgeMenuItem[] {
	const items: RunnerBadgeMenuItem[] = [
		{
			id: 'continue-here',
			label: 'Continue here',
			action: { kind: 'adopt' },
			disabled: false,
			title: 'Resume this session on this runner'
		}
	];
	if (input.isPinned) {
		items.push({
			id: 'unpin',
			label: 'Unpin',
			action: { kind: 'unpin' },
			disabled: false,
			title: 'Keep running here, but let the next send from another runner take over'
		});
	}
	const peers = peerRunners(input.installs).filter((i) => i.id !== input.ownerId);
	for (const install of peers) {
		const blocked = isBlocked(install, input.hasProject, input.eligibility);
		items.push({
			id: `move-${install.id}`,
			label: install.device_name,
			action: blocked ? null : { kind: 'move', targetInstanceId: install.id },
			disabled: blocked,
			title: runnerTitle(install, input.hasProject, input.eligibility)
		});
	}
	return items;
}

/** Query for GET /cluster/git-state. Legacy id wins; v2-only uses project_v2_id. */
export function gitStateQuery(session: {
	project_id?: string | null;
	project_v2_id?: string | null;
}): { project_id: string } | { project_v2_id: string } | null {
	if (session.project_id) return { project_id: session.project_id };
	if (session.project_v2_id) return { project_v2_id: session.project_v2_id };
	return null;
}
