/**
 * Vault: the owner's saved sign-ins, synced across workers (SSH machines and
 * web services the agent can use without seeing the secret).
 *
 * The list is the only row that renders without data. Each sign-in reaches
 * search through `searchSource()`, by its name and kind only.
 */
import type { SearchItem, SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'vault.entries',
		category: 'vault',
		section: 'Sign-ins',
		label: 'Vault sign-ins',
		keywords: [
			'vault', 'credential', 'credentials', 'password', 'username', 'sign-in', 'login', 'ssh', 'private key',
			'api token', 'secret', 'saved connection', 'machine', 'web service', 'add sign-in'
		]
	}
];

let items: { id: string; label: string; kind: string }[] = [];

export function setVaultSearchItems(list: { id: string; label: string; kind: string }[]): void {
	items = list.map((e) => ({ id: e.id, label: e.label, kind: e.kind }));
}

export function searchSource(): SearchItem[] {
	return items.map((e) => ({
		title: e.label,
		where: e.kind === 'service' ? 'Vault, Web service' : 'Vault, SSH machine',
		keywords: 'vault sign-in credential',
		target: { category: 'vault' as const, item: e.id }
	}));
}
