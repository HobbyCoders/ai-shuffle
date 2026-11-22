/**
 * Memory: the saved facts agents remember between chats, and the worker's
 * own health check of them (the Memory health item, formerly on System).
 *
 * Only the page and its fixed rows are searchable. Memory text can be
 * sensitive, so it never enters the Settings search index (no
 * `searchSource` here).
 */
import type { SettingEntry } from '../types';

/** The list item that holds the health check. */
export const MEMORY_HEALTH_ITEM = 'health';

export const entries: SettingEntry[] = [
	{
		id: 'memory.list',
		category: 'memory',
		section: 'Memory',
		label: 'Memory',
		keywords: ['memories', 'remember', 'recall', 'forget', 'saved facts', 'long-term memory', 'edit memory', 'delete memory']
	},
	{
		id: 'memory.health',
		category: 'memory',
		section: 'Memory health',
		item: MEMORY_HEALTH_ITEM,
		label: 'Memory health',
		hint: 'Checks itself every hour',
		help: 'This worker checks saved facts every hour without using a model. It can move unused or duplicate facts into an archive that Recall can search. Older facts may later be permanently deleted when that archive fills or they expire; Recall cannot find deleted facts. Facts you asked to keep are excluded from automatic archive cleanup. There is no retention setting yet.',
		keywords: ['long-term memory', 'saved facts', 'remember', 'recall', 'learner', 'duplicates', 'prune', 'archive']
	}
];
