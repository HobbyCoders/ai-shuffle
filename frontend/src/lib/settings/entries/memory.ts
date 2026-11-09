/**
 * Memory: the saved facts agents remember between chats.
 *
 * Only the page itself is searchable. Memory text can be sensitive, so it
 * never enters the Settings search index (no `searchSource` here).
 */
import type { SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'memory.list',
		category: 'memory',
		section: 'Memory',
		label: 'Memory',
		keywords: ['memories', 'remember', 'recall', 'forget', 'saved facts', 'long-term memory', 'edit memory', 'delete memory']
	}
];
