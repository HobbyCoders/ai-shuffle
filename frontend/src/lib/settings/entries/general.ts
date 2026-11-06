/**
 * General: Appearance, Conversation, Thinking and tools, Region.
 *
 * GeneralPage.svelte renders one `<SettingRow id>` per entry. Defaults match
 * the stores (chatPreferences, theme, timezonePreference), so the modified
 * edge and reset follow them.
 */
import type { SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'general.theme',
		category: 'general',
		section: 'Appearance',
		label: 'Theme',
		hint: 'Light, dark, or follow this device',
		keywords: ['dark', 'light', 'mode', 'color scheme', 'system']
	},
	{
		id: 'general.themeStyle',
		category: 'general',
		section: 'Appearance',
		label: 'Theme style',
		hint: 'A colour and type preset for the whole app',
		keywords: ['preset', 'colors', 'palette', 'swatches'],
		default: ''
	},
	{
		id: 'general.suggestReply',
		category: 'general',
		section: 'Conversation',
		label: 'Suggest next reply',
		hint: 'Offers a draft of your next message. Each one uses an extra model call',
		keywords: ['reply suggestions', 'next response', 'draft'],
		default: false
	},
	{
		id: 'general.showThinking',
		category: 'general',
		section: 'Thinking and tools',
		label: 'Show thinking blocks',
		hint: "Show the model's reasoning in the transcript",
		keywords: ['reasoning'],
		default: true
	},
	{
		id: 'general.expandThinking',
		category: 'general',
		section: 'Thinking and tools',
		label: 'Expand thinking by default',
		hint: 'Open thinking blocks instead of folding them',
		keywords: ['reasoning', 'collapse'],
		default: false
	},
	{
		id: 'general.thinkingSummary',
		category: 'general',
		section: 'Thinking and tools',
		label: 'Show thinking summary',
		hint: 'Preview folded thinking in one line',
		keywords: ['reasoning', 'preview'],
		default: true
	},
	{
		id: 'general.expandTools',
		category: 'general',
		section: 'Thinking and tools',
		label: 'Expand tool calls by default',
		hint: 'Open tool results instead of folding them',
		keywords: ['tool results', 'collapse'],
		default: false
	},
	{
		id: 'general.groupTools',
		category: 'general',
		section: 'Thinking and tools',
		label: 'Group tool calls',
		hint: 'Fold consecutive tool calls into one row',
		keywords: ['tool results', 'collapse'],
		default: true
	},
	{
		id: 'general.expandTodos',
		category: 'general',
		section: 'Thinking and tools',
		label: 'Expand todo list by default',
		hint: "Open the agent's todo list instead of folding it",
		keywords: ['tasks', 'checklist'],
		default: true
	},
	{
		id: 'general.timezone',
		category: 'general',
		section: 'Region',
		label: 'Time zone',
		hint: 'Used when scheduling automations',
		keywords: ['timezone', 'clock', 'region', 'utc'],
		default: 'local'
	}
];
