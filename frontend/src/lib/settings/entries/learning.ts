/**
 * Learned behaviour: the rules the memory learner writes about how agents
 * should work with you, its on/off switch and daily call limit.
 *
 * Rule text can be sensitive, so rules never enter the Settings search index
 * (no `searchSource` here); only the page and its two settings do.
 */
import type { SettingEntry } from '../types';

const SETUP = { category: 'learning', section: 'Learning', item: 'setup' } as const;

export const entries: SettingEntry[] = [
	{
		id: 'learning.list',
		category: 'learning',
		section: 'Learned behaviour',
		label: 'Learned behaviour',
		keywords: ['learner', 'learning', 'behaviour rules', 'behavior rules', 'learned guidance', 'corrections', 'frustration', 'how agents work']
	},
	{
		...SETUP,
		id: 'learning.enabled',
		label: 'Learn from chats',
		keywords: ['learner on', 'learner off', 'disable learner', 'turn off learning', 'stop learning', 'save tokens'],
		default: true
	},
	{
		...SETUP,
		id: 'learning.dailyCalls',
		label: 'Daily learning calls',
		hint: '0 picks a limit for your provider',
		keywords: ['learner budget', 'learning cost', 'daily calls', 'token budget', 'learner limit'],
		default: 0
	}
];
