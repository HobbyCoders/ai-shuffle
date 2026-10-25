import { describe, expect, it } from 'vitest';
import {
	availableScopes,
	canRemember,
	decisionLabels,
	rememberFor,
	scopeHint,
	scopeLabel
} from './permission-scope';

const ALL = ['none', 'session', 'profile'];
const bash = (over: Record<string, unknown> = {}) => ({ tool_name: 'Bash', remember_scopes: ALL, ...over });

describe('availableScopes', () => {
	it('lists what the server offers, in display order, always starting with once', () => {
		expect(availableScopes(bash())).toEqual(['none', 'session', 'profile']);
		expect(availableScopes(bash({ remember_scopes: ['profile', 'none'] }))).toEqual(['none', 'profile']);
		expect(availableScopes(bash({ remember_scopes: ['session'] }))).toEqual(['none', 'session']);
	});

	it('treats a missing, empty or unknown list as this request only', () => {
		expect(availableScopes({ tool_name: 'Bash' })).toEqual(['none']);
		expect(availableScopes(bash({ remember_scopes: null }))).toEqual(['none']);
		expect(availableScopes(bash({ remember_scopes: [] }))).toEqual(['none']);
		expect(availableScopes(bash({ remember_scopes: ['forever', 'global'] }))).toEqual(['none']);
		expect(canRemember({ tool_name: 'Bash' })).toBe(false);
	});

	it('never offers a kept answer for a model switch or a relayed prompt', () => {
		expect(availableScopes(bash({ tool_name: 'ModelSwitch' }))).toEqual(['none']);
		expect(availableScopes(bash({ provider_agent: { remote_approval_id: 'r1', worker_name: 'Box' } }))).toEqual(['none']);
		// A local subagent's prompt (no relay id) keeps the choice.
		expect(canRemember(bash({ provider_agent: { worker_session_id: 'c1' } }))).toBe(true);
	});
});

describe('rememberFor: the choice to the remember value sent', () => {
	it.each([
		['none', 'none'],
		['session', 'session'],
		['profile', 'profile']
	] as const)('sends %s as %s when offered', (choice, sent) => {
		expect(rememberFor(choice, bash())).toBe(sent);
	});

	it('falls back to once when the choice is not offered', () => {
		expect(rememberFor('profile', bash({ remember_scopes: ['none', 'session'] }))).toBe('none');
		expect(rememberFor('session', { tool_name: 'Bash' })).toBe('none');
		expect(rememberFor('profile', bash({ tool_name: 'ModelSwitch' }))).toBe('none');
	});
});

describe('wording', () => {
	it('labels each lifetime plainly', () => {
		expect(['none', 'session', 'profile'].map((s) => scopeLabel(s as never))).toEqual([
			'Just this once',
			'This chat until it goes idle',
			'Always for this agent'
		]);
		expect(['none', 'session', 'profile'].map((s) => scopeLabel(s as never, true))).toEqual(['Once', 'This chat', 'Always']);
	});

	it('says the agent-wide answer is permanent and covers every call of the tool', () => {
		const hint = scopeHint('profile', 'Bash');
		expect(hint).toMatch(/^Permanent\./);
		expect(hint).toContain('every Bash call');
		expect(hint).toContain('every worker');
	});

	it('says when a chat answer ends', () => {
		expect(scopeHint('session', 'Edit')).toBe('Covers every Edit call in this chat until it goes idle or the worker restarts.');
	});

	it('names the lasting effect on the buttons', () => {
		expect(decisionLabels('none')).toEqual({ allow: 'Approve', deny: 'Deny' });
		expect(decisionLabels('session')).toEqual({ allow: 'Allow in this chat', deny: 'Deny in this chat' });
		expect(decisionLabels('profile')).toEqual({ allow: 'Always allow', deny: 'Always deny' });
	});
});
