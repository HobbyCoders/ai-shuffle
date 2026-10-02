/**
 * Devices: account and remote access, approval requests, pairing, workers,
 * phones and browsers (attention: pending requests).
 *
 * Every Devices row needs loaded data, and the shared coverage test renders
 * each collection with nothing loaded, so there are no registry entries.
 * Search reaches the rows through `searchSource()`, landing on the item and
 * tab (Shuffle account, This worker > Overview or Network, each device).
 */
import { devicesModel } from '$lib/components/settings/devices/devices-model.svelte';
import { isPendingRequest, requestId } from '$lib/components/settings/devices/device-requests.svelte';
import type { Attention, SearchItem, SettingEntry } from '../types';

export const entries: SettingEntry[] = [];

const account = { category: 'devices' as const, item: 'account' };
const self = (tab: string) => ({ category: 'devices' as const, item: 'self', tab });

export function searchSource(): SearchItem[] {
	const m = devicesModel;
	const items: SearchItem[] = [
		{ title: 'Add device', where: 'Devices', keywords: 'pair pairing code qr invite worker phone browser', target: { category: 'devices' } },
		{ title: 'Shuffle account', where: 'Devices, Account', keywords: 'hobbycoders sign in login', target: account },
		{ title: 'Remote access', where: 'Devices, Shuffle account', keywords: 'tunnel connections limit', target: account },
		{ title: 'Account username', where: 'Devices, Shuffle account', keywords: 'username profile name', target: account },
		{ title: 'Rename this worker', where: 'Devices, This worker', keywords: 'worker name device name', target: self('Overview') },
		{ title: 'Join network', where: 'Devices, This worker, Network', keywords: 'invite code tailscale worker address leave and join', target: self('Network') },
		{ title: 'Keep separate settings', where: 'Devices, This worker, Network', keywords: 'own network standalone', target: self('Network') }
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
