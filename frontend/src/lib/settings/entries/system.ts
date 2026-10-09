/**
 * System: workspace folder, import from Claude Code and Codex, export all data,
 * updates, memory health, license, and Help (report a problem to HobbyCoders support). The legacy 'license' tab and AccountPanel land on `system.license`.
 * SystemPage.svelte renders one `<SettingRow id>` per entry.
 */
import type { SettingEntry } from '../types';

export const entries: SettingEntry[] = [
	{
		id: 'system.workspace',
		category: 'system',
		section: 'Workspace',
		label: 'Workspace folder',
		keywords: ['directory', 'path', 'validate', 'projects folder']
	},
	{
		id: 'system.import',
		category: 'system',
		section: 'Workspace',
		label: 'Import from Claude Code and Codex',
		hint: 'Skills, agents, MCP, projects',
		keywords: ['import settings', 'instructions', 'skills', 'agents', 'mcp', 'codex', 'claude code']
	},
	{
		id: 'system.exportAll',
		category: 'system',
		section: 'Your data',
		label: 'Export all data',
		hint: 'Every chat, agent, project',
		help: 'Downloads one ZIP: every chat as a private backup (.jsonl) and as Markdown, each agent\'s settings and your project list. AI sign-ins, API keys, tokens, settings and agents\' environment variables are never included. Chats are raw backups, so anything typed or shown in a chat is in the file: keep it private. A long history takes a few minutes.',
		keywords: ['export', 'download', 'backup', 'zip', 'all chats', 'history', 'take out', 'data access', 'markdown', 'jsonl']
	},
	{
		id: 'system.allowUpdates',
		category: 'system',
		section: 'Updates',
		label: 'Allow updates',
		keywords: ['application updates', 'auto update', 'signed'],
		default: true
	},
	{
		id: 'system.updateAll',
		category: 'system',
		section: 'Updates',
		label: 'Update every worker',
		hint: 'When you press Update',
		keywords: ['update all workers', 'scope'],
		default: false
	},
	{
		id: 'system.checkUpdates',
		category: 'system',
		section: 'Updates',
		label: 'Check for updates',
		help: 'Offline, busy or unsupported workers are reported, not queued. Docker workers otherwise check once a day. Runtime and security changes still need a new release; Android changes need a Play update.',
		keywords: ['check now', 'new version', 'restart', 'worker results']
	},
	{
		id: 'system.deployStatus',
		category: 'system',
		section: 'Updates',
		label: 'Running revisions',
		hint: 'Each worker versus main',
		help: 'Shows each worker\'s running revision, update channel and how many commits it is behind main, so you can tell whether a merged change is live there. Android loads its UI from the worker; only android/ changes need a Play build.',
		keywords: ['deploy', 'revision', 'behind', 'live', 'is it deployed', 'version drift']
	},
	{
		id: 'system.version',
		category: 'system',
		section: 'Updates',
		label: 'Version',
		hint: 'Restart from the sidebar',
		keywords: ['about', 'release', 'build']
	},
	{
		id: 'system.memoryHealth',
		category: 'system',
		section: 'Memory',
		label: 'Memory health',
		hint: 'Checks itself every hour',
		help: 'This worker checks its saved facts without using a model: whether learning works, what sessions get to see, and whether anything was lost. It archives exact duplicates and unconfirmed facts nobody used; Recall still finds them there. Facts you asked to keep are never removed.',
		keywords: ['long-term memory', 'saved facts', 'remember', 'recall', 'learner', 'duplicates', 'prune', 'archive']
	},
	{
		id: 'system.license',
		category: 'system',
		section: 'License',
		label: 'License',
		help: 'One license covers up to 3 workers. Without an active license, new agent runs pause; your sessions, files and settings always stay available.',
		keywords: ['plan', 'status', 'beta', 'lease', 'grace', 'subscription']
	},
	{
		id: 'system.seats',
		category: 'system',
		section: 'License',
		label: 'Seats',
		hint: 'Up to 3 workers',
		keywords: ['license seats', 'free a seat', 'deactivate worker']
	},
	{
		id: 'system.licenseKey',
		category: 'system',
		section: 'License',
		label: 'License key',
		keywords: ['activate', 'check now', 'use a different key']
	},
	{
		id: 'system.deactivate',
		category: 'system',
		section: 'License',
		label: 'Deactivate on this worker',
		hint: 'New agent runs pause here',
		keywords: ['license deactivate', 'free seat']
	},
	{
		id: 'system.reportProblem',
		category: 'system',
		section: 'Help',
		label: 'Report a problem',
		hint: 'Send to HobbyCoders support',
		help: 'Describe what went wrong. You then see exactly what will be sent, with secrets hidden, and nothing leaves this worker until you press Send. Sending needs this worker signed in to a Shuffle account.',
		keywords: ['support', 'bug', 'send report', 'send logs', 'feedback', 'contact', 'issue', 'help', 'hobbycoders']
	}
];
