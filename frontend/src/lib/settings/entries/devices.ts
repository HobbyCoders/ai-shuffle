/**
 * Devices: account and remote access, approval requests, pairing, workers,
 * phones and browsers (attention: pending requests).
 *
 * Fixed rows live on two items: `account` (Shuffle account) and `self`
 * (this worker, Overview and Network tabs). Other devices are dynamic and
 * reach search through `searchSource()`.
 */
import { devicesModel } from '$lib/components/settings/devices/devices-model.svelte';
import { isPendingRequest, requestId } from '$lib/components/settings/devices/device-requests.svelte';
import type { Attention, SearchItem, SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'devices.accountSignIn',
		category: 'devices',
		section: 'Shuffle account',
		item: 'account',
		label: 'Sign in',
		hint: 'Optional for local use',
		keywords: ['shuffle account', 'hobbycoders', 'login', 'phone', 'qr code']
	},
	{
		id: 'devices.username',
		category: 'devices',
		section: 'Shuffle account',
		item: 'account',
		label: 'Username',
		keywords: ['account name', 'profile']
	},
	{
		id: 'devices.remoteAccess',
		category: 'devices',
		section: 'Shuffle account',
		item: 'account',
		label: 'Remote access',
		keywords: ['tunnel', 'connections', 'limit', 'remote']
	},
	{
		id: 'devices.workerName',
		category: 'devices',
		section: 'This worker',
		item: 'self',
		tab: 'Overview',
		label: 'Name',
		keywords: ['rename', 'worker name', 'device name']
	},
	{
		id: 'devices.joinNetwork',
		category: 'devices',
		section: 'This worker, Network',
		item: 'self',
		tab: 'Network',
		label: 'Join a network',
		keywords: ['invite', 'invite code', 'tailscale', 'worker address', 'leave and join']
	},
	{
		id: 'devices.keepSeparate',
		category: 'devices',
		section: 'This worker, Network',
		item: 'self',
		tab: 'Network',
		label: 'Keep separate settings',
		hint: 'Start its own network',
		keywords: ['own network', 'standalone', 'no sync']
	}
];

export function searchSource(): SearchItem[] {
	const m = devicesModel;
	const items: SearchItem[] = [
		{ title: 'Add device', where: 'Devices', keywords: 'pair pairing code qr invite worker phone browser', target: { category: 'devices' } },
		{ title: 'Shuffle account', where: 'Devices, Account', keywords: 'sign in remote access', target: { category: 'devices', item: 'account' } }
	];
	for (const d of m.requests.devices) {
		items.push({ title: d.device_name || 'Unnamed device', where: 'Devices, Requests', keywords: 'approve deny request', target: { category: 'devices', item: requestId(d) } });
	}
	if (m.status) items.push({ title: m.workerName, where: 'Devices, This worker', keywords: 'rename network', target: { category: 'devices', item: 'self' } });
	for (const w of m.members) {
		items.push({ title: w.name, where: 'Devices, Workers', keywords: 'worker rename revoke', target: { category: 'devices', item: `member:${w.device_id}` } });
	}
	for (const c of m.clients) {
		items.push({ title: c.device_name, where: 'Devices, Phones and browsers', keywords: 'phone browser revoke', target: { category: 'devices', item: `client:${c.id}` } });
	}
	for (const d of m.accountDevices) {
		items.push({ title: d.name, where: 'Devices, On your account', keywords: 'account revoke', target: { category: 'devices', item: d.key } });
	}
	for (const i of m.legacyInstalls) {
		items.push({ title: i.device_name, where: 'Devices, Shared database', keywords: 'legacy allow revoke remove', target: { category: 'devices', item: `legacy:${i.id}` } });
	}
	return items;
}

export function attention(): Attention | null {
	const count = devicesModel.requests.devices.filter((d) => isPendingRequest(d)).length;
	return count > 0 ? { count, tone: 'warn' } : null;
}
