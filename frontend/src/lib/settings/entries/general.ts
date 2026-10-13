/**
 * General: Appearance, Conversation, Usage limits, Thinking and tools, Region.
 *
 * GeneralPage.svelte renders one `<SettingRow id>` per entry. Defaults match
 * the stores (chatPreferences, theme, timezonePreference) and, for Usage
 * limits, the server's (one pair of switches for every provider), so the
 * modified edge and reset follow them.
 */
import type { SettingEntry } from '../types';

export const LIMIT_RESUME_HELP =
	'When any provider stops a chat or a background worker at its usage limit and says when the limit resets, the chat resumes by itself shortly after that time, once. Either way, a usage limit is never retried before it resets. A chat you stopped, archived or moved is not resumed, and sending a message cancels it. Without a reset time nothing is scheduled.';
export const LIMIT_COMPACT_HELP =
	'Resuming does not make the next turn cheaper: by then the provider cache has expired, so it reads the whole conversation again. Compacting first makes that read much smaller. Applies only while Resume when the limit resets is on.';

export const entries: SettingEntry[] = [
	{
		id: 'general.theme',
		category: 'general',
		section: 'Appearance',
		label: 'Theme',
		keywords: ['dark', 'light', 'mode', 'color scheme', 'system']
	},
	{
		id: 'general.themeStyle',
		category: 'general',
		section: 'Appearance',
		label: 'Theme style',
		keywords: ['preset', 'colors', 'palette', 'swatches'],
		default: ''
	},
	{
		id: 'general.suggestReply',
		category: 'general',
		section: 'Conversation',
		label: 'Suggest next reply',
		hint: 'Uses an extra model call',
		keywords: ['reply suggestions', 'next response', 'draft'],
		default: false
	},
	{
		id: 'general.autoScroll',
		category: 'general',
		section: 'Conversation',
		label: 'Auto-scroll on new messages',
		keywords: ['scroll', 'follow'],
		default: true
	},
	{
		id: 'general.timestamps',
		category: 'general',
		section: 'Conversation',
		label: 'Show timestamps',
		keywords: ['message time', 'clock', 'date'],
		default: false
	},
	{
		id: 'general.compact',
		category: 'general',
		section: 'Conversation',
		label: 'Compact message view',
		keywords: ['density', 'spacing'],
		default: false
	},
	{
		id: 'general.limitResume',
		category: 'general',
		section: 'Usage limits',
		label: 'Resume when the limit resets',
		hint: 'Every provider, once',
		help: LIMIT_RESUME_HELP,
		keywords: ['usage limit', 'session limit', 'rate limit', 'resume', 'reset', '429', 'auto resume', 'providers'],
		default: false
	},
	{
		id: 'general.limitCompact',
		category: 'general',
		section: 'Usage limits',
		label: 'Compact before resuming',
		hint: 'Recommended',
		help: LIMIT_COMPACT_HELP,
		keywords: ['usage limit', 'compact', 'resume', 'cache', 'cost'],
		default: true
	},
	{
		id: 'general.showThinking',
		category: 'general',
		section: 'Thinking and tools',
		label: 'Show thinking blocks',
		keywords: ['reasoning'],
		default: true
	},
	{
		id: 'general.expandThinking',
		category: 'general',
		section: 'Thinking and tools',
		label: 'Expand thinking by default',
		keywords: ['reasoning', 'collapse'],
		default: false
	},
	{
		id: 'general.thinkingSummary',
		category: 'general',
		section: 'Thinking and tools',
		label: 'Show thinking summary',
		keywords: ['reasoning', 'preview'],
		default: true
	},
	{
		id: 'general.expandTools',
		category: 'general',
		section: 'Thinking and tools',
		label: 'Expand tool calls by default',
		keywords: ['tool results', 'collapse'],
		default: false
	},
	{
		id: 'general.groupTools',
		category: 'general',
		section: 'Thinking and tools',
		label: 'Group tool calls',
		keywords: ['tool results', 'collapse'],
		default: true
	},
	{
		id: 'general.expandTodos',
		category: 'general',
		section: 'Thinking and tools',
		label: 'Expand todo list by default',
		keywords: ['tasks', 'checklist'],
		default: true
	},
	{
		id: 'general.timezone',
		category: 'general',
		section: 'Region',
		label: 'Time zone',
		keywords: ['timezone', 'clock', 'region', 'utc'],
		default: 'local'
	}
];
