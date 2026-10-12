import { describe, expect, it } from 'vitest';
import type { ActiveSessionState } from '../active-session';
import type { SessionAttention } from './ws-message-handlers';
import { dismissSettledSessionPrompts, snapshotSessionPrompts } from './settle-prompts';

// #2324: settle dismisses the session's waits locally. The settle PATCH can
// resolve after the settle frame AND after another device unsettled the
// session and started a turn with a new prompt; that prompt must survive.
function harness(sessionId = 's1') {
	let attention = new Map<string, SessionAttention>();
	let state = {
		sessionId,
		pendingPermissions: [],
		deferredPermissions: [],
		pendingQuestions: [],
	} as unknown as ActiveSessionState;
	const deps = {
		backgroundPermissions: { update: (fn: (m: Map<string, SessionAttention>) => Map<string, SessionAttention>) => { attention = fn(attention); } },
		getState: () => state,
		update: (fn: (s: ActiveSessionState) => ActiveSessionState) => { state = fn(state); },
	};
	return {
		deps,
		get attention() { return attention; },
		get state() { return state; },
		setAttention(entry: SessionAttention | null) {
			const next = new Map(attention);
			if (entry) next.set(sessionId, entry); else next.delete(sessionId);
			attention = next;
		},
		addPermission(requestId: string) {
			state = { ...state, pendingPermissions: [...state.pendingPermissions, { request_id: requestId, tool_name: 'Bash' } as never] };
		},
		addQuestion(requestId: string) {
			state = { ...state, pendingQuestions: [...state.pendingQuestions, { request_id: requestId } as never] };
		},
	};
}

describe('settle prompt dismissal', () => {
	it('settle frame, then reactivation with a new permission, then the late PATCH keeps the new permission', () => {
		const h = harness();
		h.setAttention({ count: 1, kind: 'permission' });
		h.addPermission('old');

		// Client A sends the settle: snapshot what it may dismiss.
		const snap = snapshotSessionPrompts('s1', h.attention, h.state);

		// The settle frame lands first and clears everything.
		dismissSettledSessionPrompts('s1', h.deps);
		expect(h.attention.has('s1')).toBe(false);
		expect(h.state.pendingPermissions).toEqual([]);

		// Client B unsettles and starts a turn; its permission reaches A.
		h.setAttention({ count: 1, kind: 'permission' });
		h.addPermission('new');

		// A's settle PATCH finally resolves.
		dismissSettledSessionPrompts('s1', h.deps, snap);
		expect(h.attention.get('s1')).toEqual({ count: 1, kind: 'permission' });
		expect(h.state.pendingPermissions.map((p) => p.request_id)).toEqual(['new']);
	});

	it('the PATCH clears what it snapshotted when no frame beat it', () => {
		const h = harness();
		h.setAttention({ count: 2, kind: 'question' });
		h.addPermission('p1');
		h.addQuestion('q1');
		const snap = snapshotSessionPrompts('s1', h.attention, h.state);

		dismissSettledSessionPrompts('s1', h.deps, snap);
		expect(h.attention.has('s1')).toBe(false);
		expect(h.state.pendingPermissions).toEqual([]);
		expect(h.state.pendingQuestions).toEqual([]);
	});

	it('a question that arrives mid-PATCH survives while the snapshotted permission goes', () => {
		const h = harness();
		h.setAttention({ count: 1, kind: 'permission' });
		h.addPermission('p1');
		const snap = snapshotSessionPrompts('s1', h.attention, h.state);

		h.setAttention({ count: 2, kind: 'question' });
		h.addQuestion('q-new');

		dismissSettledSessionPrompts('s1', h.deps, snap);
		expect(h.attention.get('s1')).toEqual({ count: 2, kind: 'question' });
		expect(h.state.pendingPermissions).toEqual([]);
		expect(h.state.pendingQuestions.map((q) => q.request_id)).toEqual(['q-new']);
	});

	it('a background session snapshot records no prompt ids', () => {
		const h = harness('open');
		h.addPermission('p-open');
		const snap = snapshotSessionPrompts('other', h.attention, h.state);
		expect(snap.requestIds.size).toBe(0);
		dismissSettledSessionPrompts('other', h.deps, snap);
		expect(h.state.pendingPermissions).toHaveLength(1);
	});
});
