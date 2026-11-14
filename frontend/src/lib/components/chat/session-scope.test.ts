/**
 * #371 step 4: what a session shows follows one fact, whether it has a
 * project. These pin the rules the composer, the new-session hero and the attach
 * sheet all read from session-scope.ts.
 */
import { describe, expect, it } from 'vitest';
import {
	ATTACH_GAINS,
	accessModes,
	attachBlockedReason,
	attachChoice,
	attachWarning,
	effectiveAccessMode,
	noProjectPermission,
	noProjectPillVisible,
	type AttachState
} from './session-scope';

const values = (opts: { hasProject: boolean; autoSupported: boolean }) => accessModes(opts).map((m) => m.value);

describe('Access without a project', () => {
	it('offers the same modes without a project, Accept Edits included (#641)', () => {
		expect(values({ hasProject: false, autoSupported: true })).toEqual([
			'default', 'auto', 'acceptEdits', 'bypassPermissions'
		]);
		expect(values({ hasProject: false, autoSupported: false })).toEqual([
			'default', 'acceptEdits', 'bypassPermissions'
		]);
	});

	it('keeps every option, Accept Edits included, for a project session', () => {
		expect(values({ hasProject: true, autoSupported: true })).toEqual(['default', 'auto', 'acceptEdits', 'bypassPermissions']);
		expect(values({ hasProject: true, autoSupported: false })).toEqual(['default', 'acceptEdits', 'bypassPermissions']);
	});

	it('labels the modes the way the dock always has', () => {
		expect(accessModes({ hasProject: true, autoSupported: true }).map((m) => m.label)).toEqual([
			'Ask first', 'Auto', 'Accept Edits', 'Bypass'
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

	it('lets an idle session attach, and says why otherwise', () => {
		expect(attachBlockedReason(idle)).toBeNull();
		expect(attachBlockedReason({ ...idle, busy: true })).toBe('Wait for this turn to finish, then attach.');
		expect(attachBlockedReason({ ...idle, hasProject: true })).toBe('This session can’t take a project.');
	});

	it('lets a session another worker runs attach while that worker is online', () => {
		const remote = { ...idle, isRemote: true, ownerName: 'Worker B' };
		expect(attachBlockedReason(remote)).toBeNull();
		expect(attachBlockedReason({ ...remote, ownerOffline: true })).toBe('Worker B is offline. It must be online to attach a project.');
		expect(attachBlockedReason({ ...remote, ownerOffline: true, ownerName: null })).toMatch(/^The worker that runs this session is offline/);
		// The other blocks still hold.
		expect(attachBlockedReason({ ...remote, busy: true })).toBe('Wait for this turn to finish, then attach.');
		expect(attachBlockedReason({ ...remote, subagent: true })).toBe('This session can’t take a project.');
	});

	describe('which projects the sheet can pick', () => {
		const holder = (install_id: string, device_name: string, is_self = false) => ({ install_id, device_name, online: true, is_self });
		const catalog = [
			{ project_v2_id: 'v2-both', eligible: [holder('dev-a', 'Worker A', true), holder('dev-b', 'Worker B')] },
			{ project_v2_id: 'v2-a', eligible: [holder('dev-a', 'Worker A', true)] },
			{ project_v2_id: 'v2-b', eligible: [holder('dev-b', 'Worker B')] },
			{ project_v2_id: 'v2-c', eligible: [holder('dev-c', 'Worker C')] },
		];
		const local = (v2: string | null) => ({ remoteOnly: false, projectV2Id: v2 });
		const remote = (v2: string) => ({ remoteOnly: true, projectV2Id: v2 });
		const ownerB = { id: 'dev-b', name: 'Worker B', online: true };

		it('offers this worker’s projects for a session it runs', () => {
			expect(attachChoice(local('v2-a'), catalog, null)).toEqual({ pickable: true, note: null });
			expect(attachChoice(local(null), catalog, null)).toEqual({ pickable: true, note: null });
			expect(attachChoice(remote('v2-c'), catalog, null)).toEqual({ pickable: false, note: 'on Worker C' });
		});

		it('offers the owning worker’s projects for a session another worker runs', () => {
			expect(attachChoice(local('v2-both'), catalog, ownerB).pickable).toBe(true);
			expect(attachChoice(remote('v2-b'), catalog, ownerB).pickable).toBe(true);
			// This worker has it, the owner does not: greyed, saying where it is.
			expect(attachChoice(local('v2-a'), catalog, ownerB)).toEqual({ pickable: false, note: 'on this worker' });
			expect(attachChoice(remote('v2-c'), catalog, ownerB)).toEqual({ pickable: false, note: 'on Worker C' });
			// Not in the catalog at all: say it is not on the owner.
			expect(attachChoice(local(null), catalog, ownerB)).toEqual({ pickable: false, note: 'not on Worker B' });
		});

		it('offers nothing while the owning worker is offline', () => {
			expect(attachChoice(remote('v2-b'), catalog, { ...ownerB, online: false })).toEqual({ pickable: false, note: null });
		});
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
