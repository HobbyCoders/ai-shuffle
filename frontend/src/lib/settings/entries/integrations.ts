/**
 * Integrations: GitHub and API users.
 *
 * Only rows that render without data are registry entries (the shared
 * coverage test renders each collection with nothing loaded). Rows inside a
 * detail (GitHub, each API user) reach search through `searchSource()`,
 * which lands on the item and tab.
 */
import type { SearchItem, SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'integrations.apiUsers',
		category: 'integrations',
		section: 'API users',
		label: 'API users',
		keywords: ['api', 'api key', 'connector', 'discord', 'bot', 'rotate key', 'external integration']
	}
];

/** API users known to the last load; the collection keeps it current. */
let apiUserItems: { id: string; name: string }[] = [];

export function setApiUserSearchItems(users: { id: string; name: string }[]): void {
	apiUserItems = users.map((u) => ({ id: u.id, name: u.name }));
}

const github = { category: 'integrations' as const, item: 'github' };

export function searchSource(): SearchItem[] {
	return [
		{ title: 'GitHub', where: 'Integrations, Accounts', keywords: 'gh git sign in', target: github },
		{ title: 'Connect GitHub', where: 'Integrations, GitHub', keywords: 'gh personal access token pat web sign-in', target: github },
		{ title: 'Disconnect GitHub', where: 'Integrations, GitHub', keywords: 'gh sign out logout', target: github },
		...apiUserItems.flatMap((u) => [
			{
				title: u.name,
				where: 'Integrations, API users',
				keywords: 'api user connector',
				target: { category: 'integrations' as const, item: u.id }
			},
			{
				title: 'Rotate API key',
				where: `Integrations, ${u.name}`,
				keywords: 'api key regenerate replace',
				target: { category: 'integrations' as const, item: u.id }
			}
		])
	];
}
