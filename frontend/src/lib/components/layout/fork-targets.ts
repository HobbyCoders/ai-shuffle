/**
 * The workers a chat can be forked to from its title menu.
 *
 * Every approved worker except the one that runs the chat (forking there is
 * the plain "Fork session"). This worker is a target too when the chat runs
 * on another one. Offline workers are listed, greyed, like the runner badge.
 */

import type { Install } from '$lib/api/installs';
import { workerDisplayName } from '$lib/components/ui/worker-name';
import { matches } from '$lib/stores/worker-presence';

export interface ForkTarget {
	id: string;
	label: string;
	online: boolean;
}

export function forkTargets(
	installs: readonly Install[] | null | undefined,
	ownerId: string | null | undefined,
	isRemote: boolean
): ForkTarget[] {
	if (!installs) return [];
	return installs
		.filter((i) => i.approved !== false)
		.filter((i) => (isRemote && ownerId ? !matches(i, ownerId) : !i.is_self))
		.map((i) => ({
			id: i.id,
			label: workerDisplayName(i.device_name, i.is_self ? 'This worker' : 'Worker'),
			online: i.is_self ? true : i.online
		}));
}
