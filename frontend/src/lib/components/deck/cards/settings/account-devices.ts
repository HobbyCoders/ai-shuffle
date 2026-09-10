import type { AccountProfile } from '$lib/api/account';
import type { Install } from '$lib/api/installs';
import type { NetworkStatus } from '$lib/api/network';
import type { PairedClient } from '$lib/api/pairing';

export interface AccountDeviceRow {
	key: string;
	id: string;
	registration: 'servers' | 'devices';
	current?: boolean;
	localKey?: string;
	name: string;
	kind: 'Worker' | 'Phone' | 'Browser' | 'Device';
	platform?: string;
}

function endpoint(value: string): string {
	return value.replace(/\/+$/, '');
}

/** Associate registrations with local rows by identity, never by display name. */
export function accountRegistrations(profile: AccountProfile | null, status: NetworkStatus | null, installs: Install[], clients: PairedClient[]): AccountDeviceRow[] {
	if (!profile) return [];
	const endpoints = new Map<string, string>();
	for (const install of installs.filter((item) => !item.device_id)) {
		if (install.base_url) endpoints.set(endpoint(install.base_url), `legacy:${install.id}`);
	}
	for (const member of (status?.members ?? []).filter((item) => item.status === 'active')) {
		for (const url of member.endpoints) endpoints.set(endpoint(url), member.is_self ? 'self' : `member:${member.device_id}`);
	}
	const clientIds = new Map(clients.map((client) => [client.device_id, `client:${client.id}`]));
	return [
		...profile.servers.map((worker) => ({
			key: `account-worker:${worker.id}`, id: worker.id, registration: 'servers' as const, current: worker.current,
			localKey: worker.current && status ? 'self' : worker.url ? endpoints.get(endpoint(worker.url)) : undefined,
			name: worker.name || 'Unnamed worker', kind: 'Worker' as const
		})),
		...(profile.devices ?? []).map((device) => ({
			key: `account-device:${device.id}`, id: device.id, registration: 'devices' as const,
			localKey: clientIds.get(device.id) ?? (device.local_device_id ? clientIds.get(device.local_device_id) : undefined),
			name: device.name || 'Unnamed device', platform: device.platform,
			kind: /browser|web|pwa/i.test(device.platform) ? 'Browser' as const : /android|ios|iphone|mobile/i.test(device.platform) ? 'Phone' as const : 'Device' as const
		}))
	];
}
