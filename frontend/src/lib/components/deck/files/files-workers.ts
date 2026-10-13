/**
 * The Files page's worker choices, derived from GET /cluster/installs.
 *
 * Same naming as the composer's runner picker and the sidebar Device filter:
 * this worker is "This device", peers are listed by name (unapproved and
 * revoked ones are left out), and an offline peer stays listed but cannot be
 * picked. Options are keyed by the
 * owner device id, never by name: two workers may share a name, so those get
 * a short disambiguating hint.
 */
import type { Install } from '$lib/api/installs';
import { peerRunners } from '$lib/components/layout/execution-mode-eligibility';

export interface WorkerOption {
	/** Owner device id, or null for the worker serving this page. */
	key: string | null;
	name: string;
	/** Disambiguates two workers with the same name (platform or short id). */
	hint: string | null;
	isSelf: boolean;
	online: boolean;
	disabled: boolean;
	/** Why it cannot be picked, short enough for one line. */
	reason: string | null;
}

export const SELF_LABEL = 'This device';

function deviceKey(install: Install): string {
	return install.device_id || install.id;
}

export function workerOptions(installs: Install[]): WorkerOption[] {
	const self = installs.find((i) => i.is_self);
	const peers = peerRunners(installs).filter((i) => !i.revoked_at);
	const options: WorkerOption[] = [
		{
			key: null,
			name: self?.device_name || SELF_LABEL,
			hint: null,
			isSelf: true,
			online: true,
			disabled: false,
			reason: null
		}
	];
	for (const peer of peers) {
		options.push({
			key: deviceKey(peer),
			name: peer.device_name || 'Unnamed worker',
			hint: null,
			isSelf: false,
			online: !!peer.online,
			disabled: !peer.online,
			reason: peer.online ? null : 'Offline'
		});
	}
	// Same name twice: platform when it tells them apart, else the id's tail.
	const byName = new Map<string, number>();
	for (const o of options) byName.set(o.name.toLowerCase(), (byName.get(o.name.toLowerCase()) ?? 0) + 1);
	for (const o of options) {
		if ((byName.get(o.name.toLowerCase()) ?? 0) < 2) continue;
		if (o.isSelf) {
			o.hint = SELF_LABEL.toLowerCase();
			continue;
		}
		const install = peers.find((p) => deviceKey(p) === o.key);
		const platform = install?.platform ?? null;
		const samePlatform = options.filter((x) => {
			if (x === o || x.name.toLowerCase() !== o.name.toLowerCase() || x.isSelf) return false;
			return peers.find((p) => deviceKey(p) === x.key)?.platform === platform;
		}).length;
		o.hint = platform && samePlatform === 0 ? platform : `…${(o.key ?? '').slice(-4)}`;
	}
	return options;
}

/** The option for `workerId`, or undefined when that worker is not listed. */
export function findWorker(options: WorkerOption[], workerId: string | null): WorkerOption | undefined {
	return options.find((o) => o.key === workerId);
}
