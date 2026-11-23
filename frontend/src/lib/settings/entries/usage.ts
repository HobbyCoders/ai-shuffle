/**
 * Usage: Spending (the owner's daily spending limit and job time limit, which
 * moved here from the retired Assistant page, #371 step 5) and Usage limits
 * (resume after a provider's usage limit resets, #1831), and PR watch (what a
 * watched pull request's agent may do and spend; app/core/pr_watch.py).
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
	'When any provider stops a chat or a background worker at its usage limit and says when the limit resets, the chat resumes by itself shortly after that time, once. Either way, a usage limit is never retried before it resets. A chat you stopped, settled or moved is not resumed, and sending a message cancels it. Without a reset time nothing is scheduled.';
export const LIMIT_COMPACT_HELP =
	'Resuming does not make the next turn cheaper: by then the provider cache has expired, so it reads the whole conversation again. Compacting first makes that read much smaller. Applies only while Resume when the limit resets is on.';

export const PR_WATCH_MODE_HELP =
	'What happens when a watched pull request needs attention: CI failed, a reviewer with write access commented, or the branch has a conflict. Fix it wakes the chat that opened the PR to push fixes and answer reviews; it never merges, closes or force-pushes. Triage wakes it to assess and suggest only. Notify only uses no model at all and puts a note in the bell. Checking GitHub never uses a model.';
export const PR_WATCH_WAKES_HELP =
	'The most times one watched PR may wake its agent in 24 hours. Events after that go to the bell. Each wake usually re-reads the whole chat, because the provider cache has expired by then.';
export const PR_WATCH_SPEND_HELP =
	'The most one watched PR may spend on wakes in 24 hours, in US dollars at API prices (subscription use is an estimate). The daily spending limit above still applies.';
export const PR_WATCH_REPEATS_HELP =
	'When the same check fails this many times in a row, the watch stops waking the agent and asks you instead. It wakes again once the check passes.';
export const PR_WATCH_IDLE_HELP =
	'A watch ends by itself after this many days with no new commits, comments or CI results. It also ends when the PR is merged or closed.';
export const PR_WATCH_BOTS_HELP =
	'Comments from people with write access can wake the agent. Bot comments wake it only when the bot is listed here (for example a code review bot). Comments from anyone else only notify you, because on a public repository anyone can comment.';

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
	},
	{
		id: 'usage.prWatchMode',
		category: 'usage',
		section: 'PR watch',
		label: 'When a watched PR needs attention',
		hint: 'Never merges',
		help: PR_WATCH_MODE_HELP,
		keywords: ['pull request', 'pr', 'watch', 'ci', 'review', 'github', 'agent', 'fix'],
		default: 'fix'
	},
	{
		id: 'usage.prWatchWakes',
		category: 'usage',
		section: 'PR watch',
		label: 'Wakes per day',
		hint: 'Each watched PR',
		help: PR_WATCH_WAKES_HELP,
		keywords: ['pull request', 'pr watch', 'wakes', 'limit', 'cost'],
		default: 5
	},
	{
		id: 'usage.prWatchSpend',
		category: 'usage',
		section: 'PR watch',
		label: 'Spending per day',
		hint: 'Each watched PR',
		help: PR_WATCH_SPEND_HELP,
		keywords: ['pull request', 'pr watch', 'budget', 'dollars', 'cost', 'limit'],
		default: 3
	},
	{
		id: 'usage.prWatchRepeats',
		category: 'usage',
		section: 'PR watch',
		label: 'Ask me after the same check fails',
		help: PR_WATCH_REPEATS_HELP,
		keywords: ['pull request', 'pr watch', 'ci', 'failing', 'retries', 'loop'],
		default: 3
	},
	{
		id: 'usage.prWatchIdle',
		category: 'usage',
		section: 'PR watch',
		label: 'End a quiet watch after',
		help: PR_WATCH_IDLE_HELP,
		keywords: ['pull request', 'pr watch', 'idle', 'expire', 'days'],
		default: 7
	},
	{
		id: 'usage.prWatchBots',
		category: 'usage',
		section: 'PR watch',
		label: 'Bots that may wake the agent',
		hint: 'GitHub logins',
		help: PR_WATCH_BOTS_HELP,
		keywords: ['pull request', 'pr watch', 'bots', 'reviewer', 'trusted', 'comments'],
		default: ''
	}
];
