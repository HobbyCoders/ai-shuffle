/**
 * Hot-path hints in the v2 reducer.
 *
 * `mayHaveLiveRetry` and `mayHaveRunningSubagents` let per-delta and per-turn
 * sweeps skip a whole-transcript pass. They are one-sided: a false hint must
 * guarantee there is nothing for the sweep to change. These tests fold realistic
 * logs and check that guarantee after EVERY event, plus the behaviors the sweeps
 * exist for.
 */

import { describe, it, expect } from 'vitest';
import { reduce, initialState, type V2SessionState, type WireEvent } from './reducer';
import type { ChatMessage } from '../../types';

let _seq = 0;
function ev(type: string, fields: Record<string, unknown> = {}): WireEvent {
	_seq += 1;
	return {
		type,
		id: (fields.id as string) ?? `evt-${_seq}`,
		seq: (fields.seq as number) ?? _seq,
		session_id: 's1',
		created_at: '2026-07-12T00:00:00.000Z',
		...fields,
	} as WireEvent;
}

function liveRetry(m: ChatMessage): boolean {
	if (m.role !== 'system' || m.systemSubtype !== 'provider_retry') return false;
	const d = m.systemData as Record<string, unknown> | undefined;
	return !!d && (!!d.resuming || (!d.exhausted && !d.settled));
}

function runningSubagent(m: ChatMessage): boolean {
	return m.type === 'subagent' && m.agentStatus === 'running';
}

/** Fold the log, asserting the one-sided hints after every event. */
function foldChecked(events: WireEvent[], from: V2SessionState = initialState()): V2SessionState {
	let s = from;
	for (const e of events) {
		s = reduce(s, e);
		if (!s.mayHaveLiveRetry) expect(s.messages.some(liveRetry), `after ${e.type}`).toBe(false);
		if (!s.mayHaveRunningSubagents) {
			expect(s.messages.some(runningSubagent), `after ${e.type}`).toBe(false);
		}
	}
	return s;
}

function retryNote(id: string, attempt: number, max = 10): WireEvent {
	return ev('system.note', {
		id,
		subtype: 'provider_retry',
		data: { attempt, max_attempts: max },
	});
}

describe('reducer hot-path hints', () => {
	it('lowers the retry hint once a reply retires the card, and raises it for the next sequence', () => {
		const answered = foldChecked([
			ev('turn.started'),
			retryNote('pr1', 1),
			retryNote('pr1', 2),
			ev('assistant.text_delta', { id: 'd1', text: 'hi' }),
		]);
		expect(answered.mayHaveLiveRetry).toBe(false);
		const card = answered.messages.find((m) => m.id === 'pr1')!;
		expect(card.systemData).toMatchObject({ attempt: 2, settled: true });

		// Later deltas leave the transcript alone.
		const later = foldChecked([ev('assistant.text_delta', { id: 'd1', text: ' there' })], answered);
		expect(later.messages.find((m) => m.id === 'pr1')).toBe(card);

		const next = foldChecked(
			[ev('turn.completed'), ev('turn.started'), retryNote('pr2', 1)],
			later
		);
		expect(next.mayHaveLiveRetry).toBe(true);
		const done = foldChecked([ev('turn.completed')], next);
		expect(done.messages.find((m) => m.id === 'pr2')!.systemData).toMatchObject({ settled: true });
	});

	it('keeps the invariant through exhaustion, resume arming, recovery, supersede and rewind', () => {
		const s = foldChecked([
			ev('turn.started'),
			retryNote('pr1', 10),
			ev('turn.aborted', { reason: 'error', message: 'giving up' }),
			ev('turn.started'),
			ev('turn.aborted', { reason: 'interrupt' }),
			ev('turn.started'),
			ev('assistant.thinking_delta', { id: 't1', text: 'ok' }),
			ev('turn.completed'),
			ev('turn.started'),
			retryNote('pr2', 10),
			ev('turn.aborted', { reason: 'error', message: 'again' }),
			ev('message.user', { content: 'something else' }),
			ev('turn.started'),
			retryNote('pr3', 1),
			ev('transcript.rewound', { from_seq: _seq }),
			ev('assistant.text_delta', { id: 'd9', text: 'after rewind' }),
		]);
		const byId = new Map(s.messages.map((m) => [m.id, m.systemData as Record<string, unknown>]));
		expect(byId.get('pr1')).toMatchObject({ exhausted: true, recovered: true });
		expect(byId.get('pr2')).toMatchObject({ exhausted: true, superseded: true });
		expect(byId.has('pr3')).toBe(false);
	});

	it('a resume turn arms the spent card, and the reply recovers it', () => {
		const armed = foldChecked([
			ev('turn.started'),
			retryNote('pr1', 10),
			ev('turn.aborted', { reason: 'error', message: 'giving up' }),
			ev('assistant.text_delta', { id: 'stray', text: 'x' }),
			ev('turn.started'),
		]);
		expect(armed.mayHaveLiveRetry).toBe(true);
		const s = foldChecked([ev('assistant.text_delta', { id: 'd1', text: 'back' })], armed);
		expect(s.messages.find((m) => m.id === 'pr1')!.systemData).toMatchObject({
			recovered: true,
			resuming: false,
		});
	});

	it('settles a running subagent card on turn.completed, restores it on park, and reopens on progress', () => {
		const started = foldChecked([
			ev('turn.started'),
			ev('subagent.started', { task_id: 'a1', description: 'Work' }),
		]);
		expect(started.mayHaveRunningSubagents).toBe(true);

		const settled = foldChecked([ev('turn.completed')], started);
		expect(settled.mayHaveRunningSubagents).toBe(false);
		expect(settled.messages.find((m) => m.type === 'subagent')!.agentStatus).toBe('completed');

		const parked = foldChecked([ev('turn.parked')], settled);
		expect(parked.messages.find((m) => m.type === 'subagent')!.agentStatus).toBe('running');
		expect(parked.mayHaveRunningSubagents).toBe(true);

		const done = foldChecked(
			[
				ev('subagent.completed', { task_id: 'a1', status: 'completed' }),
				ev('turn.started'),
				ev('turn.completed'),
				ev('subagent.progress', { task_id: 'a1', kind: 'text', payload: { text: 'late' } }),
			],
			parked
		);
		expect(done.messages.find((m) => m.type === 'subagent')!.agentStatus).toBe('running');
		expect(done.mayHaveRunningSubagents).toBe(true);
	});

	it('turn.completed with no running card keeps the transcript array', () => {
		const s = foldChecked([
			ev('turn.started'),
			ev('assistant.text_delta', { id: 'd1', text: 'hi' }),
			ev('turn.completed'),
			ev('turn.started'),
		]);
		expect(s.mayHaveRunningSubagents).toBe(false);
		const after = reduce(s, ev('turn.completed'));
		expect(after.messages).toBe(s.messages);
	});
});
