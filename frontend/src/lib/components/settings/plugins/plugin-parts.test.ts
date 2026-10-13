import { describe, expect, it } from 'vitest';
import {
	adaptedParts,
	componentLabel,
	declaredServices,
	hookEventText,
	leftOutParts,
	runsCode,
	sourceNameFromUrl
} from './plugin-parts';

describe('componentLabel', () => {
	it('names every kind the backend lists, never as a code word', () => {
		const kinds = [
			'skill',
			'command',
			'agent',
			'mcp_server',
			'hook',
			'app',
			'lsp_server',
			'output_style',
			'user_config',
			'dependency',
			'connector_runtime',
			'service',
			'executables',
			'monitor',
			'theme',
			'workflow',
			'channel',
			'settings'
		];
		for (const kind of kinds) {
			const label = componentLabel(kind);
			expect(label).not.toContain('_');
			expect(label[0]).toBe(label[0].toUpperCase());
		}
		expect(componentLabel('executables')).toBe('Programs');
		expect(componentLabel('user_config')).toBe('Plugin settings');
	});

	it('spells out an unknown kind', () => {
		expect(componentLabel('output_filter')).toBe('Output filter');
		expect(componentLabel('statusLine')).toBe('Status line');
		expect(componentLabel('')).toBe('Other part');
	});
});

describe('runsCode', () => {
	it('lists loaded hooks and MCP servers in plain words', () => {
		const runs = runsCode([
			{ kind: 'skill', name: 's', support: 'loaded', reason: '' },
			{ kind: 'mcp_server', name: 'local', support: 'loaded', reason: '', detail: 'python srv.py' },
			{ kind: 'mcp_server', name: 'cloud', support: 'loaded', reason: '', detail: 'https://mcp.example.com' },
			{ kind: 'mcp_server', name: 'sse', support: 'unsupported', reason: 'SSE', detail: 'https://x' },
			{ kind: 'hook', name: 'SessionStart', support: 'loaded', reason: '', detail: 'echo hi' },
			{ kind: 'hook', name: 'Elsewhere', support: 'unsupported', reason: 'only hooks.json' }
		]);
		expect(runs.map((r) => [r.what, r.name, r.detail])).toEqual([
			['Runs a program', 'local', 'python srv.py'],
			['Connects to a server', 'cloud', 'https://mcp.example.com'],
			['When a chat starts', 'SessionStart', 'echo hi']
		]);
		expect(runsCode(undefined)).toEqual([]);
		expect(hookEventText('Custom')).toBe('On the Custom event');
	});
});

describe('part notes', () => {
	const parts = [
		{ kind: 'agent', name: 'a', support: 'loaded', reason: 'model pin removed' },
		{ kind: 'skill', name: 'b', support: 'loaded', reason: '' },
		{ kind: 'theme', name: 'themes', support: 'unsupported', reason: 'themes are not supported' },
		{ kind: 'user_config', name: 'k', support: 'needs_config', reason: '' },
		{ kind: 'service', name: 'cloudflare', support: 'loaded', reason: '' },
		{ kind: 'service', name: 'cloudflare', support: 'loaded', reason: '' }
	];

	it('separates adapted parts from parts left out, and services from both', () => {
		expect(adaptedParts(parts)).toEqual([{ key: 'agent:a', label: 'Agent a', note: 'model pin removed' }]);
		expect(leftOutParts(parts)).toEqual([
			{ key: 'theme:themes', label: 'Theme themes', note: 'themes are not supported' },
			{ key: 'user_config:k', label: 'Plugin settings k', note: 'Not supported here' }
		]);
		expect(declaredServices(parts)).toEqual(['cloudflare']);
		expect(adaptedParts(null)).toEqual([]);
	});
});

describe('sourceNameFromUrl', () => {
	it('takes the last path segment without .git, in letters the backend accepts', () => {
		expect(sourceNameFromUrl('https://github.com/acme/team-plugins.git')).toBe('team-plugins');
		expect(sourceNameFromUrl('https://github.com/acme/tools/')).toBe('tools');
		expect(sourceNameFromUrl('git@github.com:acme/my.plugins.git')).toBe('my-plugins');
		expect(sourceNameFromUrl('acme/skills')).toBe('skills');
		expect(sourceNameFromUrl('https://x/repo.git?ref=main')).toBe('repo');
		expect(sourceNameFromUrl('')).toBe('');
	});
});
