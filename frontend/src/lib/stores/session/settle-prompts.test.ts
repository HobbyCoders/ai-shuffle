import { afterEach, describe, expect, it } from 'vitest';
import type { ActiveSessionState } from '../active-session';
import type { SessionAttention } from './ws-message-handlers';
import { dismissSettledSessionPrompts, snapshotSessionPrompts } from './settle-prompts';
import {
	_resetProseQuestions,
	isTranscriptProseDismissed,
	observeLastUserMessage,
	proseQuestionDismissals,
	proseQuestionSessions,
	rememberLeavingProseQuestion,
} from './prose-questions';

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
		messages: [],
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
		setMessages(messages: unknown[]) {
			state = { ...state, messages } as ActiveSessionState;
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

describe('settle dismisses prose questions (#2324)', () => {
	afterEach(_resetProseQuestions);
	const asked = (seq: number) => [
		{ id: `u${seq}`, role: 'user' as const, content: 'Deploy it', seq: seq - 1, epoch: 'evt-1' },
		{ id: `a${seq}`, role: 'assistant' as const, type: 'text' as const, content: 'Staging too, or prod only?', seq, epoch: 'evt-1' },
	];

	it('a settle frame clears a background session\'s remembered prose question; a later question marks again', () => {
		const h = harness('open');
		rememberLeavingProseQuestion('bg', asked(5));
		expect(proseQuestionSessions.current.has('bg')).toBe(true);

		dismissSettledSessionPrompts('bg', h.deps);
		expect(proseQuestionSessions.current.has('bg')).toBe(false);

		// A row revision repeating the same question does not bring it back.
		observeLastUserMessage('bg', { last_assistant_question_seq: 5, last_assistant_question_epoch: 'evt-1', last_user_message_seq: 4, last_user_message_epoch: 'evt-1' });
		expect(proseQuestionSessions.current.has('bg')).toBe(false);

		// A new question (after unsettle and a new turn) has a new position.
		observeLastUserMessage('bg', { last_assistant_question_seq: 9, last_assistant_question_epoch: 'evt-1', last_user_message_seq: 8, last_user_message_epoch: 'evt-1' });
		expect(proseQuestionSessions.current.has('bg')).toBe(true);
	});

	it('dismisses the open session\'s transcript question', () => {
		const h = harness('s1');
		h.setMessages(asked(5));
		expect(isTranscriptProseDismissed(proseQuestionDismissals.current, 's1', h.state.messages)).toBe(false);

		dismissSettledSessionPrompts('s1', h.deps);
		expect(isTranscriptProseDismissed(proseQuestionDismissals.current, 's1', h.state.messages)).toBe(true);
		// A later question in the transcript is not dismissed.
		expect(isTranscriptProseDismissed(proseQuestionDismissals.current, 's1', asked(9))).toBe(false);
	});

	it('the late PATCH leaves a prose question asked after the settle was sent', () => {
		const h = harness('open');
		rememberLeavingProseQuestion('bg', asked(5));
		const snap = snapshotSessionPrompts('bg', h.attention, h.state);

		dismissSettledSessionPrompts('bg', h.deps);
		observeLastUserMessage('bg', { last_assistant_question_seq: 9, last_assistant_question_epoch: 'evt-1', last_user_message_seq: 8, last_user_message_epoch: 'evt-1' });
		expect(proseQuestionSessions.current.has('bg')).toBe(true);

		dismissSettledSessionPrompts('bg', h.deps, snap);
		expect(proseQuestionSessions.current.has('bg')).toBe(true);
	});

	// Legacy transcripts carry no seq/epoch: each question still has its own
	// identity, so dismissing one never hides a later one.
	const legacy = (n: number, text = 'Staging too, or prod only?') => [
		{ id: `u${n}`, role: 'user' as const, content: `reply ${n}` },
		{ id: `a${n}`, role: 'assistant' as const, type: 'text' as const, content: text },
	];

	it('positionless open chat: settle A, unsettle, reply, question B shows', () => {
		const h = harness('s1');
		h.setMessages(legacy(1));
		dismissSettledSessionPrompts('s1', h.deps);
		expect(isTranscriptProseDismissed(proseQuestionDismissals.current, 's1', h.state.messages)).toBe(true);

		// Unsettle clears nothing; the user replies and the agent asks again,
		// with the same words, in a new message.
		h.setMessages([...legacy(1), ...legacy(2)]);
		expect(isTranscriptProseDismissed(proseQuestionDismissals.current, 's1', h.state.messages)).toBe(false);

		// Leaving the chat remembers B, which stays marked.
		rememberLeavingProseQuestion('s1', h.state.messages);
		expect(proseQuestionSessions.current.has('s1')).toBe(true);
	});

	it('positionless: a delayed PATCH snapshot of A keeps question B', () => {
		const h = harness('s1');
		h.setMessages(legacy(1));
		const snap = snapshotSessionPrompts('s1', h.attention, h.state);
		dismissSettledSessionPrompts('s1', h.deps);

		h.setMessages([...legacy(1), ...legacy(2)]);
		dismissSettledSessionPrompts('s1', h.deps, snap);
		expect(isTranscriptProseDismissed(proseQuestionDismissals.current, 's1', h.state.messages)).toBe(false);

		// Same for a remembered positionless question of a background session.
		const bg = harness('open');
		rememberLeavingProseQuestion('bg', legacy(1));
		const bgSnap = snapshotSessionPrompts('bg', bg.attention, bg.state);
		dismissSettledSessionPrompts('bg', bg.deps);
		expect(proseQuestionSessions.current.has('bg')).toBe(false);
		rememberLeavingProseQuestion('bg', [...legacy(1), ...legacy(2)]);
		dismissSettledSessionPrompts('bg', bg.deps, bgSnap);
		expect(proseQuestionSessions.current.has('bg')).toBe(true);
	});
});
