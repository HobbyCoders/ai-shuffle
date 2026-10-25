/**
 * #371 step 4: what a session shows follows one fact, whether it has a
 * project. These pin the rules the composer, the agent card and the attach
 * sheet all read from session-scope.ts.
 */
import { describe, expect, it } from 'vitest';
import {
	ATTACH_GAINS,
	accessModes,
	attachBlockedReason,
	attachWarning,
	effectiveAccessMode,
	noProjectPermission,
	noProjectPillVisible,
	runsOnVisible,
	type AttachState
} from './session-scope';
import type { Install } from '$lib/api/installs';

const values = (opts: { hasProject: boolean; autoSupported: boolean }) => accessModes(opts).map((m) => m.value);

describe('Access without a project', () => {
	it('offers the same modes without a project, Accept Edits included (#641)', () => {
		expect(values({ hasProject: false, autoSupported: true })).toEqual(['default', 'auto', 'acceptEdits', 'bypassPermissions']);
		expect(values({ hasProject: false, autoSupported: false })).toEqual(['default', 'acceptEdits', 'bypassPermissions']);
	});

	it('keeps every option, Accept Edits included, for a project session', () => {
		expect(values({ hasProject: true, autoSupported: true })).toEqual(['default', 'auto', 'acceptEdits', 'bypassPermissions']);
		expect(values({ hasProject: true, autoSupported: false })).toEqual(['default', 'acceptEdits', 'bypassPermissions']);
	});

	it('labels the modes the way the dock always has', () => {
		expect(accessModes({ hasProject: true, autoSupported: true }).map((m) => m.label)).toEqual([
			'Ask', 'Auto', 'Accept Edits', 'Bypass'
		]);
	});

	it.each([
		['acceptEdits', false, 'acceptEdits'],
		['acceptEdits', true, 'acceptEdits'],
		['bypassPermissions', false, 'bypassPermissions'],
		['auto', false, 'auto'],
		['plan', true, 'default'],
		[null, false, 'default'],
	] as const)('shows a stored %s as %s (project: %s)', (stored, hasProject, expected) => {
		expect(effectiveAccessMode(stored, { hasProject, autoSupported: true })).toBe(expected);
	});

	it('shows Auto as Ask on a model without it', () => {
		expect(effectiveAccessMode('auto', { hasProject: false, autoSupported: false })).toBe('default');
	});

	it.each([
		[null, null],
		['default', 'default'],
		['auto', 'auto'],
		['bypassPermissions', 'bypassPermissions'],
		['acceptEdits', 'acceptEdits'],
		['plan', 'default'],
	] as const)('starts a no-project session from a last pick of %s on %s', (last, expected) => {
		expect(noProjectPermission(last)).toBe(expected);
	});
});

describe('Runs on visibility', () => {
	const self: Install = {
		id: 'self', device_name: 'ExampleWorker', base_url: null, platform: 'linux', version: null,
		capabilities: {}, last_seen: null, online: true, is_self: true
	};

	it('always shows on desktop, wherever the session runs', () => {
		expect(runsOnVisible({ phone: false, runsOnId: null, connected: self })).toBe(true);
		expect(runsOnVisible({ phone: false, runsOnId: 'self', connected: self })).toBe(true);
		expect(runsOnVisible({ phone: false, runsOnId: 'peer', connected: self })).toBe(true);
	});

	it('hides on the phone when the session runs on the connected worker', () => {
		expect(runsOnVisible({ phone: true, runsOnId: null, connected: self })).toBe(false);
		expect(runsOnVisible({ phone: true, runsOnId: '', connected: self })).toBe(false);
		expect(runsOnVisible({ phone: true, runsOnId: 'self', connected: self })).toBe(false);
	});

	it('matches a network worker picked by its instance id against its device-id row', () => {
		// A network worker's row id is its device id; the runner pick stores the
		// instance id. The same worker must still read as "here".
		const networked: Install = {
			...self, id: 'dev_example', device_id: 'dev_example', legacy_instance_id: 'inst-example'
		};
		expect(runsOnVisible({ phone: true, runsOnId: 'inst-example', connected: networked })).toBe(false);
		expect(runsOnVisible({ phone: true, runsOnId: 'dev_example', connected: networked })).toBe(false);
		expect(runsOnVisible({ phone: true, runsOnId: 'inst-workstation', connected: networked })).toBe(true);
	});

	it('shows on the phone only when it differs from the connected worker', () => {
		expect(runsOnVisible({ phone: true, runsOnId: 'peer', connected: self })).toBe(true);
		// The connected worker is not known yet: a picked peer still shows.
		expect(runsOnVisible({ phone: true, runsOnId: 'peer', connected: null })).toBe(true);
	});
});

describe('Attach a project', () => {
	const idle: AttachState = {
		sessionId: 's1', hasProject: false, isRemote: false, busy: false,
		archived: false, subagent: false, hidden: false
	};

	it('names background work when only that blocks the attach', () => {
		const base: AttachState = {
			sessionId: 's1', hasProject: false, isRemote: false, busy: false,
			archived: false, subagent: false, hidden: false
		};
		expect(attachBlockedReason({ ...base, backgroundWork: true })).toMatch(/running or background work/);
		// A running turn says so first.
		expect(attachBlockedReason({ ...base, busy: true, backgroundWork: true })).toBe('Wait for this turn to finish, then attach.');
		expect(attachBlockedReason(base)).toBeNull();
	});

	it('shows the No project pill only on a running session without a project', () => {
		expect(noProjectPillVisible(idle)).toBe(true);
		expect(noProjectPillVisible({ ...idle, sessionId: null })).toBe(false);
		expect(noProjectPillVisible({ ...idle, hasProject: true })).toBe(false);
		expect(noProjectPillVisible({ ...idle, archived: true })).toBe(false);
		expect(noProjectPillVisible({ ...idle, subagent: true })).toBe(false);
		expect(noProjectPillVisible({ ...idle, hidden: true })).toBe(false);
	});

	it('lets an idle local session attach, and says why otherwise', () => {
		expect(attachBlockedReason(idle)).toBeNull();
		expect(attachBlockedReason({ ...idle, isRemote: true })).toBe('Attach the project from the worker that runs this session.');
		expect(attachBlockedReason({ ...idle, busy: true })).toBe('Wait for this turn to finish, then attach.');
		expect(attachBlockedReason({ ...idle, hasProject: true })).toBe('This session can’t take a project.');
	});

	it('warns that attaching is one-way, naming the agent and the project', () => {
		const warning = attachWarning('Juniper', 'ai-shuffle');
		expect(warning.title).toBe('One-way. Projects can’t be detached.');
		expect(warning.body).toContain('Juniper keeps this conversation');
		expect(warning.body).toContain('ai-shuffle’s checkout');
		expect(warning.body).toContain('start a new session');
	});

	it('says the access returns to Ask, whatever the project', () => {
		expect(attachWarning('Juniper', 'ai-shuffle').access).toMatch(/^Access returns to Ask/);
		expect(attachWarning(null, null).access).toMatch(/^Access returns to Ask/);
	});

	it('still warns before a project is picked or when the agent has no name', () => {
		const warning = attachWarning('  ', null);
		expect(warning.title).toMatch(/one-way/i);
		expect(warning.body.startsWith('Your agent keeps this conversation')).toBe(true);
		expect(warning.body).toContain('the project’s checkout');
	});

	it('lists what appears after attaching', () => {
		expect(ATTACH_GAINS).toEqual(['Git panel', 'Branch', 'Checkout', 'Terminal in the checkout']);
	});
});
