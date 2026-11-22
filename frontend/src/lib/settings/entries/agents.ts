/**
 * Agent instructions: the chat instructions sent to every agent on this
 * worker. AgentsPage.svelte renders one `<SettingRow id>` per entry. The row
 * was on Security until the Settings reorganization; navigation.svelte.ts
 * redirects its old `security.secInstr` anchor here.
 */
import type { SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'agents.instructions',
		category: 'agents',
		section: 'Every chat',
		label: 'Chat instructions',
		hint: 'Sent to every agent',
		keywords: ['system prompt', 'custom instructions', 'security instructions', 'default prompt', 'markdown', 'file links', 'revert']
	}
];
