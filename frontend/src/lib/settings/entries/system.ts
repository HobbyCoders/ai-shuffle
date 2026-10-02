/**
 * System: workspace folder, import from Claude Code and Codex, updates,
 * license. The legacy 'license' tab and AccountPanel land on `system.license`.
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
		id: 'system.version',
		category: 'system',
		section: 'Updates',
		label: 'Version',
		hint: 'Restart from the sidebar',
		keywords: ['about', 'release', 'build']
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
	}
];
