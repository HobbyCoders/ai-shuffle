/**
 * Hooks: app hooks, one project's hooks, and read-only plugin hooks.
 *
 * The list is the only row that renders without data. Each hook reaches
 * search through `searchSource()`, which the collection keeps current.
 */
import type { SearchItem, SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'hooks.list',
		category: 'hooks',
		section: 'Hooks',
		label: 'Hooks',
		keywords: [
			'hook',
			'webhook',
			'bash',
			'PreToolUse',
			'PostToolUse',
			'Stop',
			'SessionStart',
			'automation',
			'add hook',
			'test hook'
		]
	}
];

interface HookSearchItem {
	id: string;
	name: string;
	event: string;
	where: string;
}

let hookItems: HookSearchItem[] = [];

export function setHookSearchItems(items: HookSearchItem[]): void {
	hookItems = items.map((h) => ({ ...h }));
}

export function searchSource(): SearchItem[] {
	return hookItems.map((h) => ({
		title: h.name,
		where: `Hooks, ${h.where}`,
		keywords: `hook ${h.event}`,
		target: { category: 'hooks' as const, item: h.id }
	}));
}
