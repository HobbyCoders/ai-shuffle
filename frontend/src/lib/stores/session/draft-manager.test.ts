/**
 * Draft manager — ephemeral composer state, system-message injection, reload
 * and the session-takeover handshake. Pure factory, driven with a fake store.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { ActiveSessionState } from '../active-session';
import { createDraftManager } from './draft-manager';

function harness(initial: Partial<ActiveSessionState> = {}) {
	let state = {
		sessionId: 'S1', messages: [], draft: '', scrollTop: 0, error: null, forkOfferSessionId: null,
		worktreeError: null, sessionLocked: false, sessionLockedBy: undefined, pendingRestorePrompt: null,
		...initial
	} as unknown as ActiveSessionState;
	const deps = {
		update: vi.fn((fn: (s: ActiveSessionState) => ActiveSessionState) => { state = fn(state); }),
		getState: () => state,
		wsSend: vi.fn(() => true),
		wsIsConnected: vi.fn(() => true),
		feedRestart: vi.fn(),
		feedRevive: vi.fn()
	};
	return { dm: createDraftManager(deps), deps, get state() { return state; } };
}

afterEach(() => {
	vi.useRealTimers();
});

describe('draft manager — ephemeral state', () => {
	it('sets draft, scroll and restore prompt', () => {
		const h = harness();
		h.dm.setDraft('hello');
		h.dm.setScrollTop(420);
		h.dm.setPendingRestorePrompt('retry this');
		expect(h.state).toMatchObject({ draft: 'hello', scrollTop: 420, pendingRestorePrompt: 'retry this' });
		h.dm.clearPendingRestorePrompt();
		expect(h.state.pendingRestorePrompt).toBeNull();
	});

	it('clearing an absent restore prompt keeps the same state object', () => {
		const h = harness();
		const before = h.state;
		h.dm.clearPendingRestorePrompt();
		expect(h.state).toBe(before);
	});

	it('clearError also drops the fork offer; clearWorktreeError is separate', () => {
		const h = harness({ error: 'Session is locked', forkOfferSessionId: 'S0', worktreeError: 'dirty tree' } as Partial<ActiveSessionState>);
		h.dm.clearError();
		expect(h.state).toMatchObject({ error: null, forkOfferSessionId: null, worktreeError: 'dirty tree' });
		h.dm.clearWorktreeError();
		expect(h.state.worktreeError).toBeNull();
	});

	it('clearMessages empties the transcript', () => {
		const h = harness({ messages: [{ id: 'm1' }] } as unknown as Partial<ActiveSessionState>);
		h.dm.clearMessages();
		expect(h.state.messages).toEqual([]);
	});
});

describe('draft manager — system messages', () => {
	beforeEach(() => {
		vi.useFakeTimers();
		vi.setSystemTime(new Date('2026-09-25T12:00:00Z'));
	});

	it('appends a system message with its subtype', () => {
		const h = harness();
		h.dm.addSystemMessage('Rewound 3 turns', 'rewind_warning');
		expect(h.state.messages).toHaveLength(1);
		expect(h.state.messages[0]).toMatchObject({ role: 'system', type: 'system', content: 'Rewound 3 turns', systemSubtype: 'rewind_warning' });
	});

	it('gives messages added in the same millisecond distinct ids', () => {
		// The transcript is a keyed each block: a repeated id breaks rendering.
		const h = harness();
		h.dm.addSystemMessage('first');
		h.dm.addSystemMessage('second');
		const ids = h.state.messages.map((m) => m.id);
		expect(new Set(ids).size).toBe(2);
		expect(ids.every((id) => id.startsWith('system-'))).toBe(true);
	});
});

describe('draft manager — reload and takeover', () => {
	it('reloadSession restarts the feed only when a session is bound', () => {
		const h = harness();
		h.dm.reloadSession();
		expect(h.deps.feedRestart).toHaveBeenCalledWith('S1');
		const none = harness({ sessionId: null });
		none.dm.reloadSession();
		expect(none.deps.feedRestart).not.toHaveBeenCalled();
	});

	it('rewindToMessage is a no-op (truncation arrives over SSE)', () => {
		const h = harness();
		h.dm.rewindToMessage('m1');
		expect(h.deps.update).not.toHaveBeenCalled();
		expect(h.deps.feedRestart).not.toHaveBeenCalled();
	});

	it('takeover unlocks, sends a forced load and resumes the feed without a reset', () => {
		const h = harness({ sessionLocked: true, sessionLockedBy: 'phone', error: 'locked' } as Partial<ActiveSessionState>);
		h.dm.takeOverSession();
		expect(h.state).toMatchObject({ sessionLocked: false, sessionLockedBy: undefined, error: null });
		expect(h.deps.wsSend).toHaveBeenCalledWith({ type: 'load_session', session_id: 'S1', force: true });
		expect(h.deps.feedRevive).toHaveBeenCalledWith('S1');
		expect(h.deps.feedRestart).not.toHaveBeenCalled();
	});

	it('takeover still resumes the feed when the socket is down', () => {
		const h = harness({ sessionLocked: true } as Partial<ActiveSessionState>);
		h.deps.wsIsConnected.mockReturnValue(false);
		h.dm.takeOverSession();
		expect(h.deps.wsSend).not.toHaveBeenCalled();
		expect(h.deps.feedRevive).toHaveBeenCalledWith('S1');
	});

	it('takeover without a session does nothing', () => {
		const h = harness({ sessionId: null, sessionLocked: true } as Partial<ActiveSessionState>);
		h.dm.takeOverSession();
		expect(h.state.sessionLocked).toBe(true);
		expect(h.deps.feedRevive).not.toHaveBeenCalled();
	});
});
