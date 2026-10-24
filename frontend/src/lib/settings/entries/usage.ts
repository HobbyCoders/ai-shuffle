/**
 * Usage: Spending (the owner's daily spending limit and job time limit, which
 * moved here from the retired Assistant page, #371 step 5) and Usage limits
 * (resume after a provider's usage limit resets, #1831).
 *
 * UsagePage.svelte renders one `<SettingRow id>` per entry. Spending defaults
 * match the server's (app/db/spending_limits.py); Usage limits defaults match
 * the server's one pair of switches for every provider.
 */
import type { SettingEntry } from '../types';

export const SPEND_LIMIT_HELP =
	'Counts every chat that starts or directs other work, and every job those start (background agents and the chats they open, on any worker). Spend is in US dollars at API prices, so subscription use is an estimate. At the limit, new background work waits until the next day and running jobs are stopped. The day starts at midnight in your time zone (Settings > General > Time zone).';
export const JOB_LIMIT_HELP =
	'A background job that runs longer than this is stopped. Turns you started yourself are never stopped.';
export const LIMIT_RESUME_HELP =
	'When any provider stops a chat or a background worker at its usage limit and says when the limit resets, the chat resumes by itself shortly after that time, once. Either way, a usage limit is never retried before it resets. A chat you stopped, archived or moved is not resumed, and sending a message cancels it. Without a reset time nothing is scheduled.';
export const LIMIT_COMPACT_HELP =
	'Resuming does not make the next turn cheaper: by then the provider cache has expired, so it reads the whole conversation again. Compacting first makes that read much smaller. Applies only while Resume when the limit resets is on.';

export const entries: SettingEntry[] = [
	{
		id: 'usage.spentToday',
		category: 'usage',
		section: 'Spending',
		label: 'Spent today',
		hint: 'Resets at midnight',
		keywords: ['cost', 'money', 'spend', 'budget', 'dollars', 'today']
	},
	{
		id: 'usage.spendLimit',
		category: 'usage',
		section: 'Spending',
		label: 'Daily spending limit',
		hint: 'Coordinated work and its jobs',
		help: SPEND_LIMIT_HELP,
		keywords: ['spending cap', 'budget', 'cost limit', 'money', 'assistant limit', 'daily cap'],
		default: true
	},
	{
		id: 'usage.spendAmount',
		category: 'usage',
		section: 'Spending',
		label: 'Limit per day',
		hint: 'US dollars, API prices',
		keywords: ['spending cap', 'budget', 'amount', 'dollars'],
		default: 20
	},
	{
		id: 'usage.jobLimit',
		category: 'usage',
		section: 'Spending',
		label: 'Longest background job',
		hint: 'Applies to each job',
		help: JOB_LIMIT_HELP,
		keywords: ['job time', 'timeout', 'run time', 'background job', 'minutes'],
		default: true
	},
	{
		id: 'usage.jobMinutes',
		category: 'usage',
		section: 'Spending',
		label: 'Stop a job after',
		keywords: ['job time', 'timeout', 'run time', 'hours', 'minutes'],
		default: '120'
	},
	{
		id: 'usage.limitResume',
		category: 'usage',
		section: 'Usage limits',
		label: 'Resume when the limit resets',
		hint: 'Every provider, once',
		help: LIMIT_RESUME_HELP,
		keywords: ['usage limit', 'session limit', 'rate limit', 'resume', 'reset', '429', 'auto resume', 'providers'],
		default: false
	},
	{
		id: 'usage.limitCompact',
		category: 'usage',
		section: 'Usage limits',
		label: 'Compact before resuming',
		hint: 'Recommended',
		help: LIMIT_COMPACT_HELP,
		keywords: ['usage limit', 'compact', 'resume', 'cache', 'cost'],
		default: true
	}
];
