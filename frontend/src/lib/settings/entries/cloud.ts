/**
 * Cloud: the AI Shuffle Cloud account and plan. Local use is free; the account
 * only sets how many remote workers the hub serves. Replaces System > License;
 * old `license` links and `system.license*` anchors land here
 * (navigation.svelte.ts). CloudPage.svelte renders one `<SettingRow id>` per entry.
 */
import type { SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'cloud.account',
		category: 'cloud',
		section: 'Account',
		label: 'Cloud account',
		help: 'Everything on your own machines and network is free and needs no account. A free Cloud account adds one remote worker with no card; a paid plan adds three, plus sync across networks.',
		keywords: ['license', 'licence', 'plan', 'status', 'subscription', 'account', 'hobbycoders', 'free', 'paid', 'beta', 'lease']
	},
	{
		id: 'cloud.link',
		category: 'cloud',
		section: 'Account',
		label: 'Link an account',
		hint: 'One remote worker, no card',
		keywords: ['sign in', 'link account', 'shuffle account', 'connect', 'free account']
	},
	{
		id: 'cloud.remoteWorkers',
		category: 'cloud',
		section: 'Plan',
		label: 'Remote workers',
		keywords: ['seats', 'remote access', 'tunnel', 'connections', 'limit', 'free a seat']
	},
	{
		id: 'cloud.plan',
		category: 'cloud',
		section: 'Plan',
		label: 'Plan',
		keywords: ['founder', 'monthly', 'yearly', 'subscription', 'paid plan']
	},
	{
		id: 'cloud.renewal',
		category: 'cloud',
		section: 'Plan',
		label: 'Paid access until',
		keywords: ['lease renewal', 'renew', 'expires', 'expired', 'grace', 'payment', 'check now', 'refresh', 'seat lost']
	},
	{
		id: 'cloud.upgrade',
		category: 'cloud',
		section: 'Plan',
		label: 'Upgrade',
		hint: 'Three remote workers, sync',
		keywords: ['pricing', 'buy', 'paid plan', 'price', 'upgrade']
	},
	{
		id: 'cloud.key',
		category: 'cloud',
		section: 'Plan',
		label: 'Plan key',
		keywords: ['license key', 'activate', 'ASH key', 'use a different key']
	},
	{
		id: 'cloud.deactivate',
		category: 'cloud',
		section: 'Plan',
		label: 'Deactivate on this worker',
		hint: 'Frees its paid seat',
		keywords: ['license deactivate', 'free seat', 'release']
	}
];
