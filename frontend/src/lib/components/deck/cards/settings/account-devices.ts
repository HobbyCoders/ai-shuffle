import type { AccountProfile } from '$lib/api/account';
import type { Install } from '$lib/api/installs';
import type { NetworkStatus } from '$lib/api/network';
import type { PairedClient } from '$lib/api/pairing';

export interface AccountDeviceRow {
	key: string;
	name: string;
	kind: 'Worker' | 'Phone' | 'Browser' | 'Device';
	platform?: string;
}

function endpoint(value: string): string {
	return value.replace(/\/+$/, '');
}

/** Keep account registrations that do not already have a local device row.
 * Names are deliberately not identities: two devices can have the same name.
 */
export function accountOnlyDevices(profile: AccountProfile | null, status: NetworkStatus | null, installs: Install[], clients: PairedClient[]): AccountDeviceRow[] {
	if (!profile) return [];
	const endpoints = new Set([
		...(status?.members ?? []).filter((member) => member.status === 'active').flatMap((member) => member.endpoints),
		...installs.filter((install) => !install.device_id).flatMap((install) => install.base_url ? [install.base_url] : [])
	].map(endpoint));
	const clientIds = new Set(clients.map((client) => client.device_id));
	return [
		...profile.servers.filter((worker) => !(worker.current && status) && !(worker.url && endpoints.has(endpoint(worker.url)))).map((worker) => ({
			key: `account-worker:${worker.id}`, name: worker.name || 'Unnamed worker', kind: 'Worker' as const
		})),
		...(profile.devices ?? []).filter((device) => !clientIds.has(device.id) && !(device.local_device_id && clientIds.has(device.local_device_id))).map((device) => ({
			key: `account-device:${device.id}`, name: device.name || 'Unnamed device', platform: device.platform,
			kind: /browser|web|pwa/i.test(device.platform) ? 'Browser' as const : /android|ios|iphone|mobile/i.test(device.platform) ? 'Phone' as const : 'Device' as const
		}))
	];
}
