/**
 * Security: who can reach this worker. Sign-in (owner password, trusted LAN)
 * and terminals opened from other workers. The agent rows (chat instructions,
 * computer use, agents run as you) moved to Agents (entries/agents.ts).
 * SecurityPage.svelte renders one `<SettingRow id>` per entry.
 */
import type { SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'security.ownerPassword',
		category: 'security',
		section: 'Sign-in',
		label: 'Owner password',
		keywords: ['password', 'change', 'set', 'owner access', 'sign in']
	},
	{
		id: 'security.trustedLan',
		category: 'security',
		section: 'Sign-in',
		label: 'Trusted LAN, no login',
		hint: 'Network devices skip password',
		help: 'Any browser that can reach this address becomes the owner without a password: guests on your Wi-Fi, other people on your tailnet, and any compromised device. Leave this off unless your network is only yours.',
		keywords: ['tailnet', 'lan', 'wifi', 'no password'],
		default: false
	},
	{
		id: 'security.peerTerminals',
		category: 'security',
		section: 'Terminals on other workers',
		label: 'Terminals on other workers',
		hint: 'Off by default',
		help: 'Lets a chat on another worker of yours open its shell from the app you are using. Turn it on for both workers. Each device links once per worker with a code, then signs every shell it opens. Agents, API keys and preview links cannot use it.',
		keywords: ['terminal', 'shell', 'peer', 'remote', 'device link', 'other worker'],
		default: false
	}
];
