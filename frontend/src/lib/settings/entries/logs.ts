/**
 * Logs: log entries (attention: errors in the lines Logs last loaded).
 */
import { logsModel } from '$lib/components/settings/logs/logs-model.svelte';
import type { Attention, SearchItem, SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'logs.level',
		category: 'logs',
		section: 'Logs',
		label: 'Log level',
		keywords: ['debug', 'info', 'warning', 'error', 'critical', 'filter']
	},
	{
		id: 'logs.export',
		category: 'logs',
		section: 'Logs',
		label: 'Export logs',
		keywords: ['download', 'file', 'save', 'ai-shuffle-logs']
	}
];

export function searchSource(): SearchItem[] {
	return [
		{ title: 'Errors', where: 'Logs', keywords: 'error critical failed exception', target: { category: 'logs' } },
		{ title: 'Search logs', where: 'Logs', keywords: 'find message filter', target: { category: 'logs' } }
	];
}

export function attention(): Attention | null {
	if (!logsModel.loaded) return null;
	const count = logsModel.errorCount;
	return count > 0 ? { count, tone: 'err' } : null;
}
