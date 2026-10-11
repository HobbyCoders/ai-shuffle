import { describe, expect, it } from 'vitest';
import type { Session } from '$lib/api/client';
import type { ChatMessage } from '$lib/stores/types';
import {
	cardActions,
	withinTapTarget,
	sessionNeedsYou,
	needsYouCount,
	openSessionNeeds,
	NO_OPEN_NEEDS,
	REMEMBERED_PROSE_NEEDS,
	sessionNeedsInput,
	attentionTitle,
	deriveGitStage,
	deriveStatus,
	diffCounts,
	isAttentionStatus,
	isLiveStatus,
	isTurnLiveStatus,
	isRemovedDeviceSession,
	isRemovedProjectSession,
	isVisibleSidebarSession,
	liveProjectKeys,
	prStateText,
	sessionPrState,
	sessionPullRequests,
	summarizePullRequests,
	sessionDeviceId,
	sessionModeOf,
	sessionTimestamp,
	UNKNOWN_REMOTE_DEVICE,
	sessionWorkMs,
	GIT_STAGE_LABELS,
	GIT_STAGE_TONES,
	GIT_STAGE_TIPS,
	RUNTIME_STATUS_TONES,
	RUNTIME_STATUS_WORDS,
	runtimeStatusWord,
	STATUS_LABELS,
	type GitStage,
} from './session-card-data';
import { needsYouTurnIds } from '$lib/utils/transcript-navigation';

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

describe('owner runtime state', () => {
	it.each([
		['working', 'working'], ['parked', 'parked'], ['permission', 'needs_permission'],
		['question', 'has_question'], ['idle', 'done'], ['offline', 'offline'], ['unknown', 'unknown'],
	] as const)('%s is authoritative over stale viewer state', (remoteRuntimeState, expected) => {
		expect(deriveStatus({ remoteRuntimeState, streaming: true, backgroundWork: true,
			parked: true, attention: { count: 1, kind: 'permission' }, isActive: true })).toBe(expected);
	});
	it('a send from this tab wins over a stale remote idle', () => {
		// The owner republishes "working" only after it claims the turn; until
		// then the card must not fall back to Idle/Done right after sending.
		expect(deriveStatus({ remoteRuntimeState: 'idle', streaming: true, optimisticStreaming: true,
			attention: null, isActive: true })).toBe('working');
	});
	it.each([
		['working', 'working'], ['parked', 'parked'], ['permission', 'needs_permission'],
		['question', 'has_question'], ['offline', 'offline'], ['unknown', 'unknown'],
	] as const)('a send from this tab does not override remote %s', (remoteRuntimeState, expected) => {
		expect(deriveStatus({ remoteRuntimeState, streaming: true, optimisticStreaming: true,
			attention: null, isActive: true })).toBe(expected);
	});
	// #1807: a pending private-access request outranks the owner's run state,
	// which reads `working` while the RequestChatAccess call waits.
	it.each(['working', 'idle', 'unknown', null] as const)('a waiting private-access request reads as needing the owner over %s', (remoteRuntimeState) => {
		expect(deriveStatus({ remoteRuntimeState, streaming: true, privateAccess: true,
			attention: null, isActive: true })).toBe('needs_permission');
	});
	it('keeps git stage independent after a remote turn settles', () => {
		expect(deriveStatus({ remoteRuntimeState: 'idle', streaming: true,
			attention: null, isActive: true, prState: 'open' })).toBe('pr');
	});
});

describe('runtimeStatusWord', () => {
	it('says Compacting for a working session whose context is being compacted', () => {
		expect(runtimeStatusWord('working', true)).toBe('Compacting');
		expect(runtimeStatusWord('working')).toBe(RUNTIME_STATUS_WORDS.working);
		// Only a running turn can be compacting; other states keep their word.
		expect(runtimeStatusWord('done', true)).toBe(RUNTIME_STATUS_WORDS.done);
	});
});

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

	it('keeps a named remote project without a checkout on the viewer', () => {
		expect(isRemovedProjectSession(session({ is_remote: true, project_id: null,
			project_v2_id: 'pv2-remote', origin: 'git:remote.git', project_name: 'Remote' }), new Set())).toBe(false);
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

describe('sessionDeviceId', () => {
	const owner = 'dev_0bb34fe0a39d6fac01558ea5';

	it('keys a legacy install id by its owner device', () => {
		expect(sessionDeviceId(session({ is_remote: true, instance_id: 'ai-shuffle-43cb45e0',
			owner_device_id: owner, device_name: 'Unraid' }))).toBe(owner);
	});

	it('keys a remote session with no instance id by its owner device, not as local', () => {
		expect(sessionDeviceId(session({ is_remote: true, instance_id: null,
			owner_device_id: owner, device_name: 'Unraid' }))).toBe(owner);
	});

	it('puts every instance id shape of one worker in the same bucket', () => {
		const ids = new Set(['ai-shuffle-43cb45e0', owner, null].map((instance_id) =>
			sessionDeviceId(session({ is_remote: true, instance_id, owner_device_id: owner }))));
		expect(ids).toEqual(new Set([owner]));
	});

	it('falls back to the instance id when no owner device is known', () => {
		expect(sessionDeviceId(session({ is_remote: true, instance_id: 'peer', owner_device_id: null }))).toBe('peer');
	});

	it('keys a local session as __local__', () => {
		expect(sessionDeviceId(session({ is_remote: false, instance_id: 'self', owner_device_id: owner }))).toBe('__local__');
	});

	it('keys a remote session with neither id as an unknown remote device', () => {
		expect(sessionDeviceId(session({ is_remote: true, instance_id: null, owner_device_id: null })))
			.toBe(UNKNOWN_REMOTE_DEVICE);
	});
});

describe('deriveStatus', () => {
	const base = { streaming: false, attention: null as { count: number; kind: 'permission' | 'question' } | null, isActive: true };

	it('keeps background work ahead of PR and done fallbacks', () => {
		for (const prState of [null, 'open', 'merged', 'closed'] as const) {
			expect(deriveStatus({ ...base, backgroundWork: true, prState, openPr: true })).toBe('parked');
		}
		expect(deriveStatus({ ...base, backgroundWork: false })).toBe('done');
		expect(deriveStatus({ ...base, backgroundWork: false, prState: 'open' })).toBe('pr');
		expect(deriveStatus({ ...base, backgroundWork: false, prState: 'merged' })).toBe('pr_merged');
		expect(deriveStatus({ ...base, backgroundWork: true, isActive: false })).toBe('parked');
	});

	it('shows a turn that ended on a running shell as Parked, not Working, without a marker', () => {
		// The owner's report: a chat parked on a background shell (Monitor) read
		// "Working". Some park paths never write the marker, so the running-task
		// summary alone must read as Parked; only a running turn is Working.
		expect(deriveStatus({ ...base, backgroundWork: true, parked: false })).toBe('parked');
		expect(deriveStatus({ ...base, backgroundWork: true, streaming: true })).toBe('working');
	});

	it.each(['permission', 'question'] as const)('keeps %s ahead of background work', (kind) => {
		expect(deriveStatus({ ...base, backgroundWork: true, attention: { count: 1, kind } }))
			.toBe(kind === 'question' ? 'has_question' : 'needs_permission');
	});

	it('distinguishes parked work from streaming and completed work', () => {
		expect(deriveStatus({ ...base, parked: true, backgroundWork: true })).toBe('parked');
		expect(deriveStatus({ ...base, parked: true, streaming: true })).toBe('working');
		expect(deriveStatus({ ...base, parked: true, attention: { count: 1, kind: 'question' } })).toBe('has_question');
		expect(isLiveStatus('parked')).toBe(true);
		expect(RUNTIME_STATUS_WORDS.parked).toBe('Parked');
	});

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

describe('sessionModeOf', () => {
	it('reads a pre-modes row (no `mode` at all) as the code lane', () => {
		// The column only ever held NULL or 'voice' before this feature, so every
		// historical row must land in Code rather than in no lane at all.
		expect(sessionModeOf(session())).toBe('code');
		expect(sessionModeOf(session({ mode: undefined }))).toBe('code');
	});

	it('passes the three known lanes through', () => {
		expect(sessionModeOf(session({ mode: 'code' }))).toBe('code');
		expect(sessionModeOf(session({ mode: 'chat' }))).toBe('chat');
		expect(sessionModeOf(session({ mode: 'voice' }))).toBe('voice');
	});

	it('falls back to code for a value it does not recognise', () => {
		// A row written by a newer install (or garbage) must stay reachable in
		// some lane instead of disappearing from all three lists.
		expect(sessionModeOf(session({ mode: 'telepathy' }))).toBe('code');
		expect(sessionModeOf(session({ mode: '' }))).toBe('code');
	});
});

describe('deriveGitStage', () => {
	const repo = { hasProject: true };
	const clean = { isRepo: true, hasChanges: false, ahead: 0, unpushed: null as number | null, defaultBranch: 'main' };
	it('preserves a squash-merged stage after remote deletion when HEAD matches base', () => {
		expect(deriveGitStage({ hasProject: true, prState: 'merged', git: {
			...clean, ahead: 2, matchesBaseTree: true
		} })).toBe('merged');
		expect(deriveGitStage({ hasProject: true, prState: 'merged', git: {
			...clean, ahead: 3, matchesBaseTree: false
		} })).toBe('committed');
	});

	it('marks a session with no repo behind it', () => {
		expect(deriveGitStage({ hasProject: false })).toBe('none');
		expect(deriveGitStage({ ...repo, mode: 'chat' })).toBe('none');
		expect(deriveGitStage({ ...repo, mode: 'voice' })).toBe('none');
		expect(deriveGitStage({ ...repo, git: { ...clean, isRepo: false } })).toBe('none');
	});

	it('keeps a recorded PR on a row with no project to read', () => {
		expect(deriveGitStage({ hasProject: false, prState: 'merged' })).toBe('merged');
		expect(deriveGitStage({ hasProject: false, prState: 'open' })).toBe('pr');
		expect(deriveGitStage({ hasProject: false, prState: 'closed' })).toBe('closed');
		expect(deriveGitStage({ ...repo, mode: 'chat', prState: 'merged' })).toBe('merged');
	});

	it.each(['merged', 'closed'] as const)(
		'keeps a persisted %s PR visible when this install cannot find the checkout',
		(prState) => {
			const sharedSession = { ...repo, prState };
			expect(deriveGitStage(sharedSession)).toBe(prState);
			expect(
				deriveGitStage({ ...sharedSession, git: { ...clean, isRepo: false } })
			).toBe(prState);
		}
	);

	it('lets an open PR own the glyph over a dirty tree', () => {
		const dirty = { ...clean, hasChanges: true };
		expect(deriveGitStage({ ...repo, prState: 'open', git: dirty })).toBe('pr');
	});

	it('lets new work after merge or close restart the git lifecycle', () => {
		const dirty = { ...clean, hasChanges: true };
		expect(deriveGitStage({ ...repo, prState: 'merged', git: dirty })).toBe('dirty');
		expect(deriveGitStage({ ...repo, prState: 'closed', git: dirty })).toBe('dirty');
		expect(
			deriveGitStage({ ...repo, prState: 'merged', git: { ...clean, ahead: 2, unpushed: 1 } })
		).toBe('committed');
		// The merged commits themselves still sit ahead of main and fully on the remote.
		expect(
			deriveGitStage({ ...repo, prState: 'merged', git: { ...clean, ahead: 2, unpushed: 0 } })
		).toBe('merged');
		expect(deriveGitStage({ ...repo, prState: 'merged', git: clean })).toBe('merged');
		expect(deriveGitStage({ ...repo, prState: 'closed', git: clean })).toBe('closed');
	});

	it('reads the working tree newest-work-first', () => {
		expect(deriveGitStage({ ...repo, git: { ...clean, hasChanges: true, ahead: 2 } })).toBe('dirty');
		// ahead>0 with no remote counterpart (unpushed=null) = local commits only
		expect(deriveGitStage({ ...repo, git: { ...clean, ahead: 2 } })).toBe('committed');
		expect(deriveGitStage({ ...repo, git: { ...clean, ahead: 2, unpushed: 1 } })).toBe('committed');
	});

	it('calls a branch pushed only when it has commits the remote already has', () => {
		// Fully pushed feature branch waiting on a PR.
		expect(
			deriveGitStage({
				...repo,
				branch: 'feat/x',
				git: { ...clean, ahead: 2, unpushed: 0 }
			})
		).toBe('pushed');
		// Brand-new worktree: clean tree, no unique commits, never pushed.
		// Must NOT read as "Pushed, no PR yet".
		expect(deriveGitStage({ ...repo, branch: 'feat/x', git: clean })).toBe('clean');
		expect(deriveGitStage({ ...repo, branch: 'main', git: clean })).toBe('clean');
		expect(deriveGitStage({ ...repo, branch: null, git: clean })).toBe('clean');
	});

	it('never guesses a tree state it was not given', () => {
		// Settled rows cost no request, so "no git facts" must read as the quiet
		// dot rather than as "pushed and waiting on a PR".
		expect(deriveGitStage({ ...repo, branch: 'feat/x' })).toBe('clean');
		expect(deriveGitStage({ ...repo, branch: 'feat/x', prState: 'merged' })).toBe('merged');
	});

	it('gives every stage a label, tone and tip', () => {
		const stages: GitStage[] = ['pr', 'merged', 'closed', 'dirty', 'committed', 'pushed', 'clean', 'none'];
		for (const stage of stages) {
			expect(GIT_STAGE_LABELS[stage]).toBeDefined();
			expect(GIT_STAGE_TONES[stage]).toBeTruthy();
			expect(GIT_STAGE_TIPS[stage]).toBeTruthy();
		}
		expect(GIT_STAGE_TONES.merged).toBe('purple');
		expect(GIT_STAGE_TONES.pr).toBe('green');
		expect(GIT_STAGE_TONES.dirty).toBe('amber');
	});
});

describe('prStateText', () => {
	it('names the number when there is one', () => {
		expect(prStateText('open', 123)).toBe('PR #123 open');
		expect(prStateText('merged', 123)).toBe('PR #123 merged');
		expect(prStateText('closed', 123)).toBe('PR #123 closed');
	});

	it('falls back to a bare PR when the number is missing', () => {
		expect(prStateText('open', null)).toBe('PR open');
		expect(prStateText('closed')).toBe('PR closed');
	});

	it('says nothing when there is no pull request', () => {
		expect(prStateText(null, 12)).toBeNull();
		expect(prStateText(undefined)).toBeNull();
	});
});

describe('diffCounts', () => {
	it('formats a diffstat with a real minus sign', () => {
		expect(diffCounts({ additions: 124, deletions: 38 })).toEqual({ plus: '+124', minus: '−38' });
	});

	it('drops an empty or missing diffstat', () => {
		expect(diffCounts({ additions: 0, deletions: 0 })).toBeNull();
		expect(diffCounts(null)).toBeNull();
		expect(diffCounts(undefined)).toBeNull();
	});
});

describe('runtime status words', () => {
	it('keeps the agent state and the PR state on separate segments', () => {
		// `pr` / `pr_merged` describe the branch, not the turn — the PR segment
		// says the rest, so the runtime word stays plain.
		expect(RUNTIME_STATUS_WORDS.pr).toBe('Done');
		expect(RUNTIME_STATUS_WORDS.pr_merged).toBe('Done');
		expect(RUNTIME_STATUS_WORDS.working).toBe('Working');
		expect(RUNTIME_STATUS_TONES.working).toBe('accent');
		expect(RUNTIME_STATUS_TONES.needs_permission).toBe('amber');
		expect(RUNTIME_STATUS_TONES.done).toBe('muted');
	});

	it('flags exactly the prompt-parked statuses for the warning glyph', () => {
		expect(isAttentionStatus('needs_permission')).toBe(true);
		expect(isAttentionStatus('has_question')).toBe(true);
		expect(isAttentionStatus('await')).toBe(true);
		expect(isAttentionStatus('working')).toBe(false);
		expect(isAttentionStatus('done')).toBe(false);
	});
});

describe('sessionWorkMs (#1619)', () => {
	const now = Date.parse('2026-10-01T12:10:00Z');

	it('returns the banked total when no turn is running', () => {
		expect(sessionWorkMs(session({ work_ms: 90_000 }), now, false)).toBe(90_000);
		expect(sessionWorkMs(session({ work_ms: 90_000 }), now, true)).toBe(90_000);
		// Rows from before the field existed read as zero.
		expect(sessionWorkMs(session({ work_ms: undefined }), now, false)).toBe(0);
		expect(sessionWorkMs(session({ work_ms: null }), now, false)).toBe(0);
	});

	it('adds the live interval up to now while the turn runs', () => {
		const s = session({ work_ms: 60_000, work_started_at: '2026-10-01T12:08:00+00:00' });
		expect(sessionWorkMs(s, now, true)).toBe(60_000 + 120_000);
		// A naive stamp is UTC, like every other server timestamp.
		const naive = session({ work_ms: 0, work_started_at: '2026-10-01T12:09:30' });
		expect(sessionWorkMs(naive, now, true)).toBe(30_000);
	});

	it('closes a stale live interval at the last activity, never past it', () => {
		const s = session({ work_ms: 60_000, work_started_at: '2026-10-01T12:08:00Z' });
		const lastActivity = Date.parse('2026-10-01T12:09:00Z');
		expect(sessionWorkMs(s, now, false, lastActivity)).toBe(120_000);
		// A start after the last activity adds nothing rather than going negative.
		expect(sessionWorkMs(s, now, false, Date.parse('2026-10-01T12:00:00Z'))).toBe(60_000);
	});

	it('ignores an unparseable start', () => {
		expect(sessionWorkMs(session({ work_ms: 5_000, work_started_at: 'nope' }), now, true)).toBe(5_000);
	});

	it('ticks only while a turn holds the live slot', () => {
		for (const s of ['working', 'needs_permission', 'has_question', 'await'] as const) {
			expect(isTurnLiveStatus(s)).toBe(true);
		}
		for (const s of ['parked', 'done', 'idle', 'pr', 'pr_merged', 'offline'] as const) {
			expect(isTurnLiveStatus(s)).toBe(false);
		}
	});
});

describe('cardActions', () => {
	it('offers Archive + Settle, then Active + Archive, then Restore', () => {
		expect(cardActions({ is_active: true, status: 'active' })).toEqual(['archive', 'settle']);
		expect(cardActions({ is_active: false, status: 'active' })).toEqual(['activate', 'archive']);
		expect(cardActions({ is_active: false, status: 'archived' })).toEqual(['restore']);
	});
});

describe('sessionNeedsYou / attentionTitle', () => {
	const local = { status: 'active', is_remote: false, remote_runtime_state: null } as const;
	it('reads the local attention map for local sessions', () => {
		expect(sessionNeedsYou(local, { count: 1 })).toBe(true);
		expect(sessionNeedsYou(local, { count: 0 })).toBe(false);
		expect(sessionNeedsYou(local, undefined)).toBe(false);
		expect(sessionNeedsYou({ ...local, status: 'archived' }, { count: 2 })).toBe(false);
	});
	it('lets the owner state decide for a remote session', () => {
		const remote = { status: 'active', is_remote: true } as const;
		expect(sessionNeedsYou({ ...remote, remote_runtime_state: 'permission' }, null)).toBe(true);
		expect(sessionNeedsYou({ ...remote, remote_runtime_state: 'idle' }, { count: 3 })).toBe(false);
	});
	it('counts a waiting private-access request for local and remote sessions (#1807)', () => {
		expect(sessionNeedsYou(local, null, 1)).toBe(true);
		expect(sessionNeedsYou({ status: 'active', is_remote: true, remote_runtime_state: 'working' }, null, 2)).toBe(true);
		expect(sessionNeedsYou(local, null, 0)).toBe(false);
		expect(sessionNeedsYou({ ...local, status: 'archived' }, null, 1)).toBe(false);
	});
	it('prefixes the count only when something is waiting', () => {
		expect(attentionTitle('AI Shuffle', 0)).toBe('AI Shuffle');
		expect(attentionTitle('AI Shuffle', 3)).toBe('(3) AI Shuffle');
	});
});

/**
 * The single "Needs you" count (transcript-organization spec §5): five
 * sources, one number, shared by the sidebar row, the TurnNavigator dot and
 * the tab title.
 */
describe('needsYouCount (spec §5)', () => {
	const local = { status: 'active', is_remote: false, remote_runtime_state: null } as const;
	const msg = (role: ChatMessage['role'], content: string, extra: Partial<ChatMessage> = {}): ChatMessage =>
		({ id: `${role}-${content}`, role, type: 'text', content, ...extra });
	const asked = [msg('user', 'Deploy it'), msg('assistant', 'Staging too, or prod only?')];
	const open = (over: Partial<{ pendingPermissions: unknown[]; pendingQuestions: unknown[]; messages: ChatMessage[] }>, group = true) =>
		openSessionNeeds({ pendingPermissions: [], pendingQuestions: [], messages: [], ...over }, group);

	it('counts each of the five sources alone as one', () => {
		// QuestionTray question, PermissionQueue permission, an assistant confirm
		// (a permission request asked in the conversation), PrivateAccessPrompt,
		// and a prose question.
		expect(needsYouCount(local, null, 0, open({ pendingQuestions: [{ request_id: 'q1' }] }))).toBe(1);
		expect(needsYouCount(local, null, 0, open({ pendingPermissions: [{ request_id: 'p1' }] }))).toBe(1);
		expect(needsYouCount(local, null, 0, open({
			pendingPermissions: [{ request_id: 'c1', assistant_confirm: { question: 'Restart the worker?' } }]
		}))).toBe(1);
		expect(needsYouCount(local, null, 1, NO_OPEN_NEEDS)).toBe(1);
		expect(needsYouCount(local, null, 0, open({ messages: asked }))).toBe(1);
		// A background session's prompts arrive through the attention map.
		expect(needsYouCount(local, { count: 1 }, 0, null)).toBe(1);
	});

	it('sums distinct items across sources', () => {
		const all = open({
			pendingQuestions: [{ request_id: 'q1' }],
			pendingPermissions: [{ request_id: 'p1' }, { request_id: 'c1', assistant_confirm: { question: 'Go?' } }],
			messages: asked
		});
		expect(needsYouCount(local, null, 2, all)).toBe(6);
		// A reconnect snapshot can restore the open session's attention entry for
		// the same prompts: the two views of one queue never double-count.
		expect(needsYouCount(local, { count: 3 }, 0, all)).toBe(4);
		expect(needsYouCount(local, { count: 4 }, 0, all)).toBe(5);
		expect(needsYouCount({ ...local, status: 'archived' }, null, 2, all)).toBe(0);
	});

	it('clears a prose question on the next user message, and when answered', () => {
		expect(open({ messages: asked }).proseQuestion).toBe(true);
		expect(open({ messages: [...asked, msg('user', 'Prod only')] }).proseQuestion).toBe(false);
		// Work after the question does not answer it; new prose that asks nothing does.
		expect(open({ messages: [...asked, msg('assistant', '', { type: 'tool_use', toolName: 'Read', toolStatus: 'complete' })] }).proseQuestion).toBe(true);
		expect(open({ messages: [...asked, msg('assistant', 'Deploying to prod.')] }).proseQuestion).toBe(false);
		// Answering a surface drops it from its queue, and the count with it.
		expect(needsYouCount(local, null, 0, open({ pendingQuestions: [] }))).toBe(0);
	});

	it('gates only the prose question on the groupToolCalls preference', () => {
		const prefOff = open({ messages: asked, pendingQuestions: [{ request_id: 'q1' }] }, false);
		expect(prefOff.proseQuestion).toBe(false);
		expect(needsYouCount(local, null, 1, prefOff)).toBe(2);
	});

	it('lets Needs you beat Working, a prose question reading as Awaiting input', () => {
		const working = { streaming: true, attention: null, isActive: true };
		expect(deriveStatus({ ...working, open: open({ messages: asked }) })).toBe('await');
		expect(deriveStatus({ ...working, open: open({ pendingQuestions: [{}] }) })).toBe('has_question');
		expect(deriveStatus({ ...working, open: open({ pendingPermissions: [{}], messages: asked }) })).toBe('needs_permission');
		expect(deriveStatus({ ...working, remoteRuntimeState: 'working', open: open({ messages: asked }) })).toBe('await');
		expect(deriveStatus({ ...working, open: NO_OPEN_NEEDS })).toBe('working');
		expect(isAttentionStatus('await')).toBe(true);
	});

	it('titles the tab with the summed count', () => {
		const total = [
			needsYouCount(local, null, 0, open({ messages: asked, pendingQuestions: [{}] })),
			needsYouCount(local, { count: 1 }, 0, null)
		].reduce((a, b) => a + b, 0);
		expect(attentionTitle('AI Shuffle', total)).toBe('(3) AI Shuffle');
		expect(attentionTitle('AI Shuffle', needsYouCount(local, null, 0, open({ messages: [...asked, msg('user', 'Prod')] })))).toBe('AI Shuffle');
	});
});

describe('session pull requests', () => {
	const pr = (number: number, state: 'open' | 'merged' | 'closed', extra: Record<string, unknown> = {}) => ({
		number,
		url: `https://github.com/o/r/pull/${number}`,
		state,
		...extra
	});

	it('reads a legacy single-PR row as a one-entry list', () => {
		expect(sessionPullRequests({ pr_number: 5, pr_url: 'u5', pr_state: 'open' })).toEqual([
			{ number: 5, url: 'u5', state: 'open', title: null, branch: null, source: 'session' }
		]);
		expect(sessionPullRequests({ pr_state: null })).toEqual([]);
	});

	it('keeps the stored list, drops bad entries and adds a missing primary', () => {
		const list = sessionPullRequests({
			pull_requests: [pr(1, 'merged', { source: 'subagent', title: 'T' }), { number: 'x', state: 'open' } as never],
			pr_number: 2,
			pr_url: 'https://github.com/o/r/pull/2',
			pr_state: 'open'
		});
		expect(list.map((p) => [p.number, p.state, p.source])).toEqual([
			[1, 'merged', 'subagent'],
			[2, 'open', 'session']
		]);
	});

	it('reads one PR exactly like the single-PR chip', () => {
		expect(summarizePullRequests(sessionPullRequests({ pull_requests: [pr(123, 'open')] }))?.text).toBe(
			prStateText('open', 123)
		);
		expect(summarizePullRequests([])).toBeNull();
	});

	it('counts several PRs by state with the noun on the first part', () => {
		const list = (states: ('open' | 'merged' | 'closed')[]) =>
			summarizePullRequests(sessionPullRequests({ pull_requests: states.map((st, i) => pr(i + 1, st)) }));
		expect(list(['open', 'open'])?.text).toBe('2 PRs open');
		expect(list(['merged', 'open', 'merged'])?.text).toBe('1 PR open · 2 merged');
		expect(list(['merged', 'merged', 'merged'])?.text).toBe('3 PRs merged');
		expect(list(['closed', 'merged'])?.text).toBe('1 PR merged · 1 closed');
		expect(list(['closed', 'merged'])?.state).toBe('merged');
		expect(list(['merged', 'open'])?.parts.map((p) => p.state)).toEqual(['open', 'merged']);
	});

	it('keeps a chat on an open PR while any PR is still open', () => {
		expect(
			sessionPrState({ pull_requests: [pr(1, 'open'), pr(2, 'merged')], pr_number: 2, pr_state: 'merged' })
		).toBe('open');
		expect(sessionPrState({ pr_state: 'closed' })).toBe('closed');
		expect(sessionPrState({})).toBeNull();
	});
});

describe('withinTapTarget', () => {
	const chip = { left: 100, top: 40, width: 80, height: 16 };
	it('grows a short target to 44 px about its centre', () => {
		// Centre y = 48; band 26..70.
		expect(withinTapTarget(chip, 140, 26)).toBe(true);
		expect(withinTapTarget(chip, 140, 70)).toBe(true);
		expect(withinTapTarget(chip, 140, 25)).toBe(false);
		expect(withinTapTarget(chip, 140, 71)).toBe(false);
	});
	it('keeps a target that is already large enough as it is', () => {
		expect(withinTapTarget(chip, 99, 48)).toBe(false);
		expect(withinTapTarget(chip, 181, 48)).toBe(false);
		expect(withinTapTarget({ left: 0, top: 0, width: 10, height: 10 }, 27, 5)).toBe(true);
		expect(withinTapTarget({ left: 0, top: 0, width: 10, height: 10 }, 28, 5)).toBe(false);
	});
});

describe('openSessionNeeds scan cost (spec §5)', () => {
	const asked: ChatMessage[] = [
		{ id: 'u1', role: 'user', content: 'Deploy it' },
		...Array.from({ length: 50 }, (_, i): ChatMessage => ({ id: `t${i}`, role: 'assistant', type: 'tool_use', toolName: 'Bash', toolId: `b${i}`, content: '' })),
		{ id: 'a1', role: 'assistant', type: 'text', content: 'Staging too, or prod only?' }
	];
	function counted(messages: ChatMessage[]) {
		const reads = { n: 0 };
		const proxy = new Proxy(messages, {
			get(target, key, receiver) {
				if (typeof key === 'string' && /^\d+$/.test(key)) reads.n++;
				return Reflect.get(target, key, receiver);
			}
		});
		return { proxy, reads };
	}

	it('scans a transcript once, however often the session object is replaced around it', () => {
		const { proxy, reads } = counted(asked);
		const first = openSessionNeeds({ messages: proxy, pendingPermissions: [], pendingQuestions: [] }, true);
		const scanned = reads.n;
		expect(scanned).toBeGreaterThan(0);
		// Draft typing / scroll: a new state object, same messages and queues.
		for (let i = 0; i < 20; i++) {
			const again = openSessionNeeds({ messages: proxy, pendingPermissions: [], pendingQuestions: [] }, true);
			expect(again).toBe(first);
		}
		expect(reads.n).toBe(scanned);
		expect(first.proseQuestion).toBe(true);
	});

	it('rescans when the transcript array changes', () => {
		const { proxy, reads } = counted(asked);
		openSessionNeeds({ messages: proxy }, true);
		const before = reads.n;
		const next = counted([...asked, { id: 'u2', role: 'user', content: 'Prod only' }]);
		expect(openSessionNeeds({ messages: next.proxy }, true).proseQuestion).toBe(false);
		expect(next.reads.n).toBeGreaterThan(0);
		expect(reads.n).toBe(before);
	});

	it('uses the remembered question while the history loads', () => {
		expect(openSessionNeeds({ messages: [], isLoadingSession: true }, true, true).proseQuestion).toBe(true);
		expect(openSessionNeeds({ messages: [], isLoadingSession: true }, false, true).proseQuestion).toBe(false);
		expect(sessionNeedsInput(false, NO_OPEN_NEEDS, true)).toBe(REMEMBERED_PROSE_NEEDS);
		expect(sessionNeedsInput(false, NO_OPEN_NEEDS, false)).toBeNull();
	});
});

describe('the three Needs-you consumers agree (spec §5)', () => {
	const messages: ChatMessage[] = [{ id: 'u1', role: 'user', content: 'Ship it' }];
	const noQueues = openSessionNeeds({ messages, pendingPermissions: [], pendingQuestions: [] }, true);
	// What each consumer computes for the open session: the sidebar row's
	// status, the tab title's count and the rail's dotted ticks.
	function consumers(row: { status: string; is_remote: boolean; remote_runtime_state: Session['remote_runtime_state'] }, attention: { count: number; kind: 'permission' | 'question' } | null) {
		const status = deriveStatus({
			remoteRuntimeState: row.is_remote ? row.remote_runtime_state ?? 'unknown' : null,
			streaming: true, attention, isActive: true, open: noQueues
		});
		const title = needsYouCount(row, attention, 0, noQueues);
		const rail = needsYouTurnIds(needsYouCount(row, attention, 0, noQueues), messages, [], 'u1');
		return { sidebar: isAttentionStatus(status), title, rail };
	}

	it('a remote session asking a question before its queue loads', () => {
		expect(consumers({ status: 'active', is_remote: true, remote_runtime_state: 'question' }, null))
			.toEqual({ sidebar: true, title: 1, rail: ['u1'] });
		expect(consumers({ status: 'active', is_remote: true, remote_runtime_state: 'working' }, null))
			.toEqual({ sidebar: false, title: 0, rail: [] });
	});

	it('a reconnect snapshot restoring the open session\'s attention entry', () => {
		expect(consumers({ status: 'active', is_remote: false, remote_runtime_state: null }, { count: 1, kind: 'permission' }))
			.toEqual({ sidebar: true, title: 1, rail: ['u1'] });
	});

	it('a remembered prose question on a background session outranks Working but not a prompt', () => {
		const base = { remoteRuntimeState: null, streaming: true, isActive: true, open: REMEMBERED_PROSE_NEEDS };
		expect(deriveStatus({ ...base, attention: null })).toBe('await');
		expect(deriveStatus({ ...base, attention: { count: 1, kind: 'permission' } })).toBe('needs_permission');
		expect(deriveStatus({ ...base, remoteRuntimeState: 'working', attention: null })).toBe('await');
	});
});
