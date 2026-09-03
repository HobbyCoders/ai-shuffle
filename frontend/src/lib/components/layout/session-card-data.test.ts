import { describe, expect, it } from 'vitest';
import type { Session } from '$lib/api/client';
import {
	deriveStatus,
	isLiveStatus,
	isRemovedDeviceSession,
	isRemovedProjectSession,
	isVisibleSidebarSession,
	liveProjectKeys,
	sessionTimestamp,
	STATUS_LABELS,
} from './session-card-data';

function session(over: Partial<Session> = {}): Session {
	return {
		id: 's1',
		profile_id: 'default',
		project_id: 'proj-1',
		title: 'Chat',
		status: 'idle',
		is_active: false,
		total_cost_usd: 0,
		total_tokens_in: 0,
		total_tokens_out: 0,
		cache_creation_tokens: 0,
		cache_read_tokens: 0,
		context_tokens: 0,
		turn_count: 1,
		tags: [],
		parent_session_id: null,
		fork_point_message_index: null,
		has_forks: false,
		created_at: '2026-08-20T12:00:00Z',
		updated_at: '2026-08-20T12:00:00Z',
		...over,
	};
}

describe('liveProjectKeys', () => {
	it('collects local ids, link groups, origins, and v2 ids', () => {
		const keys = liveProjectKeys(
			[{ id: 'local-1', link_group_id: 'lg-1' }],
			[{ origin: 'git:example.git', project_v2_id: 'pv2-1', local_project_id: 'local-1' }]
		);
		expect(keys).toEqual(new Set(['local-1', 'lg-1', 'git:example.git', 'pv2-1']));
	});
});

describe('isRemovedProjectSession', () => {
	const live = liveProjectKeys(
		[{ id: 'local-1' }],
		[{ origin: 'git:other.git', project_v2_id: 'pv2-remote', local_project_id: null }]
	);

	it('keeps ungrouped chats', () => {
		expect(isRemovedProjectSession(session({ project_id: null }), live)).toBe(false);
	});

	it('keeps a chat whose local project still exists', () => {
		expect(isRemovedProjectSession(session({ project_id: 'local-1' }), live)).toBe(false);
	});

	it('keeps a chat whose origin is still in the cluster catalog', () => {
		expect(isRemovedProjectSession(session({
			project_id: 'foreign-1',
			origin: 'git:other.git',
			project_v2_id: 'pv2-remote',
		}), live)).toBe(false);
	});

	it('hides a chat whose project is gone from both catalogs', () => {
		expect(isRemovedProjectSession(session({
			project_id: 'deleted-1',
			origin: 'git:gone.git',
			project_v2_id: 'pv2-gone',
			project_name: 'Deleted',
		}), live)).toBe(true);
	});
});

describe('isRemovedDeviceSession', () => {
	it('keeps this-device chats', () => {
		expect(isRemovedDeviceSession(session({ is_remote: false, instance_id: 'self' }))).toBe(false);
	});

	it('keeps a remote chat whose install still has a name', () => {
		expect(isRemovedDeviceSession(session({
			is_remote: true,
			instance_id: 'peer',
			device_name: 'Laptop',
		}))).toBe(false);
	});

	it('hides a remote chat whose install was deleted', () => {
		expect(isRemovedDeviceSession(session({
			is_remote: true,
			instance_id: 'gone',
			device_name: null,
		}))).toBe(true);
	});
});

describe('deriveStatus', () => {
	const base = { streaming: false, attention: null as { count: number; kind: 'permission' | 'question' } | null, isActive: true };

	it('shows Working while streaming', () => {
		expect(deriveStatus({ ...base, streaming: true })).toBe('working');
	});

	it('shows Needs Permission for a pending permission prompt', () => {
		expect(deriveStatus({ ...base, attention: { count: 1, kind: 'permission' } })).toBe('needs_permission');
	});

	it('shows Has Question for a pending user question', () => {
		expect(deriveStatus({ ...base, attention: { count: 2, kind: 'question' } })).toBe('has_question');
	});

	it('lets a parked prompt outrank an in-flight turn', () => {
		// The SDK can hold the stream open while blocked on a prompt — a session
		// parked on you is more urgent than one merely busy.
		expect(deriveStatus({ streaming: true, attention: { count: 1, kind: 'permission' }, isActive: true })).toBe('needs_permission');
	});

	it('shows PR Opened when an open PR exists for a settled session', () => {
		expect(deriveStatus({ ...base, openPr: true })).toBe('pr');
	});

	it('shows PR Opened from persisted pr_state even without the GitHub fallback', () => {
		expect(deriveStatus({ ...base, prState: 'open' })).toBe('pr');
	});

	it('shows PR Merged while the session is still Active', () => {
		expect(deriveStatus({ ...base, prState: 'merged', isActive: true })).toBe('pr_merged');
	});

	it('does not let the GitHub fallback override a persisted merged state', () => {
		expect(deriveStatus({ ...base, prState: 'merged', openPr: true })).toBe('pr_merged');
	});

	it('keeps Working over PR while the turn is still in flight', () => {
		expect(deriveStatus({ ...base, streaming: true, openPr: true })).toBe('working');
		expect(deriveStatus({ ...base, streaming: true, prState: 'merged' })).toBe('working');
	});

	it('falls through to Done and Idle', () => {
		expect(deriveStatus({ ...base })).toBe('done');
		expect(deriveStatus({ ...base, isActive: false })).toBe('idle');
	});
});

describe('status labels', () => {
	it('labels the dynamic states', () => {
		expect(STATUS_LABELS.pr_merged).toBe('PR Merged');
		expect(STATUS_LABELS.working).toBe('Working');
		expect(STATUS_LABELS.needs_permission).toBe('Needs Permission');
		expect(STATUS_LABELS.has_question).toBe('Has Question');
		expect(STATUS_LABELS.pr).toBe('PR Opened');
	});

	it('ticks the live-timer statuses', () => {
		for (const s of ['working', 'needs_permission', 'has_question'] as const) {
			expect(isLiveStatus(s)).toBe(true);
		}
		expect(isLiveStatus('done')).toBe(false);
	});
});


describe('isVisibleSidebarSession', () => {
	const live = liveProjectKeys([{ id: 'local-1' }], []);

	it('hides worker sessions even when the project is live', () => {
		expect(isVisibleSidebarSession(session({ project_id: 'local-1', hidden: true }), live)).toBe(false);
	});

	it('hides a live-project chat that belongs to a removed device', () => {
		expect(isVisibleSidebarSession(session({
			project_id: 'local-1',
			is_remote: true,
			device_name: null,
		}), live)).toBe(false);
	});

	it('keeps a local chat on a live project', () => {
		expect(isVisibleSidebarSession(session({ project_id: 'local-1' }), live)).toBe(true);
	});
});

describe('sessionTimestamp', () => {
	it('parses a naive stamp as UTC and prefers updated_at', () => {
		expect(sessionTimestamp(session({ updated_at: '2026-08-20T12:00:00' }))).toBe(
			Date.parse('2026-08-20T12:00:00Z')
		);
		expect(sessionTimestamp(session({ updated_at: null as never, created_at: '2026-08-19T12:00:00Z' })))
			.toBe(Date.parse('2026-08-19T12:00:00Z'));
		expect(sessionTimestamp(session({ updated_at: null as never, created_at: null as never }))).toBe(0);
	});

	it('memoizes per row but re-parses when the row\'s stamp changes in place', () => {
		// The sidebar sorts on this value and every card recomputes its elapsed
		// time from it each tick — it must be cheap AND never stale.
		const row = session({ updated_at: '2026-08-20T12:00:00Z' });
		expect(sessionTimestamp(row)).toBe(Date.parse('2026-08-20T12:00:00Z'));
		expect(sessionTimestamp(row)).toBe(Date.parse('2026-08-20T12:00:00Z'));

		row.updated_at = '2026-08-20T13:00:00Z';
		expect(sessionTimestamp(row)).toBe(Date.parse('2026-08-20T13:00:00Z'));
	});
});
