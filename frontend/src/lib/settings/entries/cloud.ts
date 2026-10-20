/**
 * Cloud: the AI Shuffle Cloud account and plan. Local use is free and there is
 * no license key: the plan follows the signed-in account, and it only sets how
 * many remote workers the hub serves. Replaces System > License; old `license`
 * links and `system.license*` anchors land here (navigation.svelte.ts).
 * CloudPage.svelte renders one `<SettingRow id>` per entry.
 */
import type { SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'cloud.account',
		category: 'cloud',
		section: 'Account',
		label: 'Cloud account',
		help: 'Everything on your own machines and network is free and needs no account. A free Cloud account adds two remote workers with no card; a paid plan adds four, plus sync across networks. The plan follows the account: sign in with it on another worker and that worker gets the same plan.',
		keywords: [
			'license',
			'licence',
			'license key',
			'plan key',
			'plan',
			'status',
			'subscription',
			'account',
			'hobbycoders',
			'free',
			'paid',
			'founder',
			'monthly',
			'yearly'
		]
	},
	{
		id: 'cloud.link',
		category: 'cloud',
		section: 'Account',
		label: 'Link an account',
		hint: 'Two remote workers, no card',
		keywords: ['sign in', 'link account', 'shuffle account', 'connect', 'free account', 'activate']
	},
	{
		id: 'cloud.remoteWorkers',
		category: 'cloud',
		section: 'Plan',
		label: 'Remote workers',
		keywords: ['seats', 'remote access', 'tunnel', 'connections', 'limit', 'in use']
	},
	{
		id: 'cloud.renewal',
		category: 'cloud',
		section: 'Plan',
		label: 'Renews',
		keywords: ['renewal', 'renew', 'expires', 'paid until', 'billing date']
	},
	{
		id: 'cloud.upgrade',
		category: 'cloud',
		section: 'Plan',
		label: 'Upgrade',
		hint: 'Four remote workers, sync',
		keywords: ['pricing', 'buy', 'paid plan', 'price', 'upgrade']
	}
];
