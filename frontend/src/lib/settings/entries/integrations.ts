/**
 * Integrations: GitHub and API users.
 *
 * GitHub rows live on the `github` item. API users are dynamic items, so
 * search reaches each one through `searchSource()` (fed by the collection
 * while it is loaded) and the list section through `integrations.apiUsers`.
 */
import type { SearchItem, SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'integrations.githubMethod',
		category: 'integrations',
		section: 'GitHub',
		item: 'github',
		label: 'Connect with',
		keywords: ['github', 'gh', 'sign in', 'personal access token', 'pat', 'web sign-in']
	},
	{
		id: 'integrations.githubDisconnect',
		category: 'integrations',
		section: 'GitHub',
		item: 'github',
		label: 'Disconnect GitHub',
		hint: 'Agents lose git push',
		keywords: ['github', 'gh', 'sign out', 'logout']
	},
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

export function searchSource(): SearchItem[] {
	return [
		{ title: 'GitHub', where: 'Integrations, Accounts', keywords: 'gh token sign in git', target: { category: 'integrations', item: 'github' } },
		...apiUserItems.map((u) => ({
			title: u.name,
			where: 'Integrations, API users',
			keywords: 'api user connector key rotate',
			target: { category: 'integrations' as const, item: u.id }
		}))
	];
}
