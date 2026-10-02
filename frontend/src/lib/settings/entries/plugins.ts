/**
 * Plugins: connected apps (connector runtimes and their accounts), agent
 * plugin packages, agent-authored drafts waiting for review, the catalog and
 * the plugin sources (attention: drafts needing review).
 *
 * Item ids: `plugin:<plugin id>` (a package, installed or not),
 * `connection:<connector type>` (an app and its accounts), `explore` (the
 * catalog) and `sources` (marketplaces, updates and auto-update).
 */
import type { Attention, SearchItem, SettingEntry } from '../types';
import { pluginsLive, primePending } from './plugins-live.svelte';

const SOURCES = { category: 'plugins', item: 'sources' } as const;

export const entries: SettingEntry[] = [
	{
		...SOURCES,
		id: 'plugins.updates',
		section: 'Updates',
		label: 'Check for updates',
		keywords: ['check for updates', 'update all', 'plugin updates', 'upgrade']
	},
	{
		...SOURCES,
		id: 'plugins.autoUpdate',
		section: 'Auto-update',
		label: 'Auto-update',
		hint: 'Checks on a schedule',
		keywords: ['automatic updates', 'plugin updates', 'background check']
	},
	{
		...SOURCES,
		id: 'plugins.updateInterval',
		section: 'Auto-update',
		label: 'Check every',
		keywords: ['update interval', 'hours', 'frequency']
	},
	{
		...SOURCES,
		id: 'plugins.autoInstall',
		section: 'Auto-update',
		label: 'Install updates',
		hint: 'When an update is found',
		keywords: ['auto install', 'automatic install', 'plugin updates']
	},
	{
		...SOURCES,
		id: 'plugins.sources',
		section: 'Sources',
		label: 'Sources',
		keywords: ['marketplace', 'git', 'plugin source', 'add source', 'sync', 'repository']
	}
];

/** Connector types this app ships, so search reaches them before a visit. */
export const CONNECTION_TYPES: Record<string, { name: string; description: string }> = {
	gmail: { name: 'Gmail', description: 'Read, draft and organize mail' },
	spotify: { name: 'Spotify', description: 'Find music, control playback' },
	discord: { name: 'Discord', description: 'Chat with agents in Discord' },
	telegram: { name: 'Telegram', description: 'Chat with agents in Telegram' },
	slack: { name: 'Slack', description: 'Messages and channels in Slack' },
	whatsapp: { name: 'WhatsApp', description: 'A WhatsApp business number' },
	line: { name: 'LINE', description: 'Your LINE messaging account' },
	signal: { name: 'Signal', description: 'Send and receive with Signal' },
	imessage: { name: 'iMessage', description: 'iMessage on your Mac' },
	bluebubbles: {
		name: 'BlueBubbles',
		description: 'iMessage through BlueBubbles'
	},
	msteams: {
		name: 'Microsoft Teams',
		description: 'Messages in Microsoft Teams'
	},
	hue: { name: 'Philips Hue', description: 'Control lights and scenes' },
	homeassistant: {
		name: 'Home Assistant',
		description: 'Control and monitor your home'
	},
	webhook: {
		name: 'Webhook',
		description: 'Notifications and external events'
	}
};

export function searchSource(): SearchItem[] {
	const live = pluginsLive.items;
	const seen = new Set(live.map((row) => row.item));
	const known: SearchItem[] = Object.entries(CONNECTION_TYPES)
		.filter(([type]) => !seen.has(`connection:${type}`))
		.map(([type, info]) => ({
			title: info.name,
			where: 'Plugins, Apps',
			keywords: `${info.description} connector connection account`,
			target: { category: 'plugins', item: `connection:${type}` }
		}));
	return [
		{
			title: 'Explore plugins',
			where: 'Plugins, Catalog',
			keywords: 'browse install marketplace skills connections apps',
			target: { category: 'plugins', item: 'explore' }
		},
		{
			title: 'Plugin sources',
			where: 'Plugins, Sources',
			keywords: 'marketplace git add source sync updates',
			target: SOURCES
		},
		...live.map((row) => ({
			title: row.title,
			where: row.where,
			keywords: row.keywords,
			target: { category: 'plugins' as const, item: row.item }
		})),
		...known
	];
}

export function attention(): Attention | null {
	primePending();
	const count = pluginsLive.pending;
	return count > 0 ? { count, tone: 'warn' } : null;
}
