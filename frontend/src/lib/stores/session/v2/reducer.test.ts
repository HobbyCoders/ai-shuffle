/**
 * Golden tests for the V2 session reducer (docs/V2_PHASE1_DESIGN.md §6).
 *
 * The reducer is PURE: `(state, event) -> state`, no I/O, no Date.now(). These
 * tests pin the message shapes MessageArea renders, the delta-segmentation
 * bookkeeping, optimistic reconcile, and replay determinism.
 */

import { describe, it, expect } from 'vitest';
import { reduce, reduceAll, initialState, type WireEvent } from './reducer';
import type { ChatMessage } from '../../types';

// ─── Event builders ─────────────────────────────────────────────────────────

let _seq = 0;
function ev(type: string, fields: Record<string, unknown> = {}): WireEvent {
	_seq += 1;
	return {
		type,
		id: (fields.id as string) ?? `evt-${_seq}`,
		seq: (fields.seq as number) ?? _seq,
		session_id: 's1',
		created_at: (fields.created_at as string) ?? '2026-07-12T00:00:00.000Z',
		...fields,
	} as WireEvent;
}

function fold(events: WireEvent[]) {
	return reduceAll(events);
}

// ─── Turn lifecycle ─────────────────────────────────────────────────────────

describe('turn lifecycle', () => {
	it('turn.started sets isStreaming, clears parked', () => {
		const s = reduce({ ...initialState(), parked: true }, ev('turn.started'));
		expect(s.isStreaming).toBe(true);
		expect(s.parked).toBe(false);
	});

	it('tracks liveTurnId across turn.started -> completed/aborted (#585)', () => {
		// Set on turn.started, cleared on the terminal event.
		const started = reduce(initialState(), ev('turn.started', { turn_id: 'turn-1' }));
		expect(started.liveTurnId).toBe('turn-1');
		const completed = reduce(started, ev('turn.completed', {}));
		expect(completed.liveTurnId).toBeNull();

		const aborted = reduce(
			reduce(initialState(), ev('turn.started', { turn_id: 'turn-2' })),
			ev('turn.aborted', { reason: 'interrupt' })
		);
		expect(aborted.liveTurnId).toBeNull();
	});

	it('turn.parked closes open text and marks parked (not streaming)', () => {
		const s = fold([
			ev('turn.started'),
			ev('assistant.text_delta', { text: 'hi' }),
			ev('turn.parked'),
		]);
		expect(s.isStreaming).toBe(false);
		expect(s.parked).toBe(true);
		expect(s.messages[0].streaming).toBe(false); // open text finalized
	});

	it('turn.completed stores usage and stops streaming', () => {
		const s = fold([
			ev('turn.started'),
			ev('turn.completed', { usage: { input_tokens: 10, output_tokens: 5 } }),
		]);
		expect(s.isStreaming).toBe(false);
		expect(s.usage).toEqual({ input_tokens: 10, output_tokens: 5 });
	});

	it('turn.usage updates usage without ending the turn', () => {
		const s = fold([ev('turn.started'), ev('turn.usage', { usage: { context_used: 42 } })]);
		expect(s.isStreaming).toBe(true);
		expect(s.usage).toEqual({ context_used: 42 });
	});

	it('turn.aborted(interrupt) errors a running tool card', () => {
		const s = fold([
			ev('turn.started'),
			ev('tool.use', { tool_use_id: 't1', name: 'Bash' }),
			ev('turn.aborted', { reason: 'interrupt' }),
		]);
		const tool = s.messages.find((m) => m.toolId === 't1')!;
		expect(tool.toolStatus).toBe('error');
		expect(tool.streaming).toBe(false);
		expect(s.isStreaming).toBe(false);
	});

	it('turn.aborted(interrupt) appends [Stopped] to the truncated assistant text', () => {
		const s = fold([
			ev('message.user', { content: 'go' }),
			ev('turn.started'),
			ev('assistant.text_delta', { text: 'partial answer' }),
			ev('turn.aborted', { reason: 'interrupt' }),
		]);
		const text = s.messages.find((m) => m.role === 'assistant' && m.type === 'text')!;
		expect(text.content).toBe('partial answer\n\n[Stopped]');
		expect(text.streaming).toBe(false);
	});

	it('turn.aborted(interrupt) marks the text before a running tool, not the tool card', () => {
		const s = fold([
			ev('message.user', { content: 'go' }),
			ev('turn.started'),
			ev('assistant.text_delta', { text: 'working on it' }),
			ev('tool.use', { tool_use_id: 't1', name: 'Bash' }),
			ev('turn.aborted', { reason: 'interrupt' }),
		]);
		const text = s.messages.find((m) => m.role === 'assistant' && m.type === 'text')!;
		expect(text.content).toBe('working on it\n\n[Stopped]');
		expect(s.messages.find((m) => m.toolId === 't1')!.toolStatus).toBe('error');
	});

	it('turn.aborted(interrupt) with no output this turn never stamps a previous response', () => {
		const s = fold([
			ev('message.user', { content: 'first' }),
			ev('turn.started'),
			ev('assistant.text_delta', { text: 'first answer' }),
			ev('turn.completed', {}),
			ev('message.user', { content: 'second' }),
			ev('turn.started'),
			ev('turn.aborted', { reason: 'interrupt' }),
		]);
		const texts = s.messages.filter((m) => m.role === 'assistant' && m.type === 'text');
		expect(texts).toHaveLength(1);
		expect(texts[0].content).toBe('first answer');
	});

	it('turn.aborted(error) does not append [Stopped]', () => {
		const s = fold([
			ev('message.user', { content: 'go' }),
			ev('turn.started'),
			ev('assistant.text_delta', { text: 'partial' }),
			ev('turn.aborted', { reason: 'error', message: 'boom' }),
		]);
		const text = s.messages.find((m) => m.role === 'assistant' && m.type === 'text')!;
		expect(text.content).toBe('partial');
	});

	// #606 item 2: turn.aborted(reason='error') carries `message` (turn_recorder
	// extra_payload) — surface it as an error-styled system card so a viewer on a
	// non-runner device sees WHY streaming stopped.
	it('turn.aborted(error) appends exactly one error system message with the wire message', () => {
		const s = fold([
			ev('message.user', { content: 'go' }),
			ev('turn.started'),
			ev('turn.aborted', {
				reason: 'error',
				message: "Worktree for session s1 (branch 'feat/x') is unavailable",
			}),
		]);
		const errors = s.messages.filter((m) => m.systemSubtype === 'error');
		expect(errors).toHaveLength(1);
		expect(errors[0]).toMatchObject({
			role: 'system',
			type: 'system',
			content: "Worktree for session s1 (branch 'feat/x') is unavailable",
			systemSubtype: 'error',
		});
		expect(s.isStreaming).toBe(false);
		expect(s.liveTurnId).toBeNull();
	});

	it('turn.aborted(error) repeated with an identical message is idempotent (no duplicate card)', () => {
		const abort = ev('turn.aborted', { reason: 'error', message: 'boom' });
		const s = fold([
			ev('message.user', { content: 'go' }),
			ev('turn.started'),
			abort,
			{ ...abort, id: 'evt-dup', seq: abort.seq + 1 } as WireEvent,
		]);
		expect(s.messages.filter((m) => m.systemSubtype === 'error')).toHaveLength(1);
	});

	it('turn.aborted(error) without a message appends nothing', () => {
		const s = fold([
			ev('message.user', { content: 'go' }),
			ev('turn.started'),
			ev('turn.aborted', { reason: 'error' }),
		]);
		expect(s.messages.filter((m) => m.role === 'system')).toHaveLength(0);
		expect(s.isStreaming).toBe(false);
	});

	it('turn.aborted(interrupt) never appends an error card even when a message rides along', () => {
		const s = fold([
			ev('message.user', { content: 'go' }),
			ev('turn.started'),
			ev('assistant.text_delta', { text: 'partial' }),
			ev('turn.aborted', { reason: 'interrupt', message: 'Query was interrupted' }),
		]);
		expect(s.messages.filter((m) => m.systemSubtype === 'error')).toHaveLength(0);
		const text = s.messages.find((m) => m.role === 'assistant' && m.type === 'text')!;
		expect(text.content).toBe('partial\n\n[Stopped]');
	});

	it('turn.aborted(error) card participates in replay determinism (reduceAll twice)', () => {
		const log = [
			ev('message.user', { content: 'go' }),
			ev('turn.started'),
			ev('turn.aborted', { reason: 'error', message: 'spawn failed' }),
		];
		expect(reduceAll(log)).toEqual(reduceAll(log));
	});
});

// ─── User messages + optimistic reconcile ───────────────────────────────────

describe('message.user reconcile', () => {
	it('appends a fresh user message with the event id', () => {
		const s = fold([ev('message.user', { id: 'srv-1', content: 'hello', command_id: null })]);
		expect(s.messages).toHaveLength(1);
		expect(s.messages[0]).toMatchObject({ id: 'srv-1', role: 'user', type: 'text', content: 'hello' });
	});

	it('reconciles onto an optimistic message by command_id, keeping the optimistic id', () => {
		const optimistic: ChatMessage = {
			id: 'local-abc',
			role: 'user',
			type: 'text',
			content: 'hello',
			commandId: 'req-xyz',
			timestamp: '2026-07-12T00:00:00.000Z',
		};
		const seeded = { ...initialState(), messages: [optimistic] };
		const s = reduce(seeded, ev('message.user', { id: 'srv-9', content: 'hello', command_id: 'req-xyz' }));
		expect(s.messages).toHaveLength(1);
		expect(s.messages[0].id).toBe('local-abc'); // kept for stable keying
		expect(s.messages[0].commandId).toBe('req-xyz');
	});

	it('reconciles by content + recency when no command_id matches', () => {
		const optimistic: ChatMessage = {
			id: 'local-def',
			role: 'user',
			type: 'text',
			content: 'ping',
			timestamp: '2026-07-12T00:00:05.000Z',
		};
		const seeded = { ...initialState(), messages: [optimistic] };
		// event 10s later, same content, no command_id → reconcile in place
		const s = reduce(
			seeded,
			ev('message.user', { id: 'srv-10', content: 'ping', command_id: null, created_at: '2026-07-12T00:00:15.000Z' })
		);
		expect(s.messages).toHaveLength(1);
		expect(s.messages[0].id).toBe('local-def');
	});

	it('does NOT reconcile stale content beyond the 30s window (appends instead)', () => {
		const optimistic: ChatMessage = {
			id: 'local-old',
			role: 'user',
			type: 'text',
			content: 'ping',
			timestamp: '2026-07-12T00:00:00.000Z',
		};
		const seeded = { ...initialState(), messages: [optimistic] };
		const s = reduce(
			seeded,
			ev('message.user', { id: 'srv-11', content: 'ping', command_id: null, created_at: '2026-07-12T00:05:00.000Z' })
		);
		expect(s.messages).toHaveLength(2);
	});
});

// ─── Task-notification wake prompts ───────────────────────────────────────────

// Mirrors app/core/turn_scheduler.py::_build_task_notification_prompt output.
function taskNotificationBlock(o: {
	taskId?: string;
	toolUseId?: string;
	outputFile?: string;
	status?: string;
	summary?: string;
	result?: string;
	subagentTokens?: string;
	toolUses?: string;
	durationMs?: string;
}): string {
	return (
		'<task-notification>\n' +
		`  <task-id>${o.taskId ?? 'task-1'}</task-id>\n` +
		`  <tool-use-id>${o.toolUseId ?? 'toolu_1'}</tool-use-id>\n` +
		`  <output-file>${o.outputFile ?? '/tmp/out.txt'}</output-file>\n` +
		`  <status>${o.status ?? 'completed'}</status>\n` +
		`  <summary>${o.summary ?? 'Did the thing'}</summary>\n` +
		'  <note>Background work has come to rest.</note>\n' +
		`  <result>${o.result ?? 'All done.'}</result>\n` +
		'  <usage>\n' +
		`    <subagent_tokens>${o.subagentTokens ?? '88851'}</subagent_tokens>\n` +
		`    <tool_uses>${o.toolUses ?? '57'}</tool_uses>\n` +
		`    <duration_ms>${o.durationMs ?? '312000'}</duration_ms>\n` +
		'  </usage>\n' +
		'</task-notification>'
	);
}

describe('task-notification wake prompts', () => {
	it('renders a single <task-notification> block as one system card (no user bubble)', () => {
		const content = taskNotificationBlock({
			// The backend does NOT XML-escape field values (turn_scheduler.py::_esc
			// is a plain str() passthrough), so literal markdown containing
			// entity-like text must survive verbatim, unescaped.
			result: 'Fixed the bug. To emphasize, use &lt;strong&gt; here instead of *bold*.',
		});
		const s = fold([ev('message.user', { id: 'srv-tn', content })]);
		expect(s.messages).toHaveLength(1);
		const m = s.messages[0];
		expect(m.role).toBe('system');
		expect(m.type).toBe('system');
		expect(m.systemSubtype).toBe('task_notification');
		expect(m.id).toBe('srv-tn');
		expect(s.messages.some((x) => x.role === 'user')).toBe(false);
		const data = m.systemData as Record<string, unknown>;
		expect(data.status).toBe('completed');
		expect(data.summary).toBe('Did the thing');
		expect(data.result).toBe('Fixed the bug. To emphasize, use &lt;strong&gt; here instead of *bold*.');
		expect(data.task_id).toBe('task-1');
		expect(data.output_file).toBe('/tmp/out.txt');
		expect(data.usage).toEqual({ subagent_tokens: 88851, tool_uses: 57, duration_ms: 312000 });
	});

	it('renders TWO concatenated blocks as two system cards with distinct stable ids', () => {
		const content =
			taskNotificationBlock({ taskId: 'task-a', summary: 'first' }) +
			'\n' +
			taskNotificationBlock({ taskId: 'task-b', summary: 'second' });
		const s = fold([ev('message.user', { id: 'srv-multi', content })]);
		expect(s.messages).toHaveLength(2);
		expect(s.messages.every((m) => m.systemSubtype === 'task_notification')).toBe(true);
		expect(s.messages[0].id).toBe('srv-multi-tn-0');
		expect(s.messages[1].id).toBe('srv-multi-tn-1');
		expect(s.messages[0].id).not.toBe(s.messages[1].id);
		expect((s.messages[0].systemData as Record<string, unknown>).summary).toBe('first');
		expect((s.messages[1].systemData as Record<string, unknown>).summary).toBe('second');
	});

	it('falls through to a normal user message when the block is truncated/unclosed', () => {
		const content = taskNotificationBlock({}).replace('</task-notification>', '');
		const s = fold([ev('message.user', { id: 'srv-trunc', content })]);
		expect(s.messages).toHaveLength(1);
		expect(s.messages[0].role).toBe('user');
		expect(s.messages[0].content).toBe(content);
	});

	it('leaves a normal user message (no marker) unchanged', () => {
		const s = fold([ev('message.user', { id: 'srv-plain', content: 'hello world' })]);
		expect(s.messages).toHaveLength(1);
		expect(s.messages[0]).toMatchObject({ role: 'user', type: 'text', content: 'hello world' });
	});
});

// ─── Quiet-pump wake notifications (subagent-step + "ok" ack suppression) ──────

// Mirrors app/core/turn_scheduler.py::_build_task_notification_prompt(kind="pump"):
// a nested <kind>pump</kind> right after the opening tag and NO <usage> block.
function pumpNotificationBlock(
	o: { taskId?: string; status?: string; summary?: string; result?: string } = {}
): string {
	return (
		'<task-notification>\n' +
		'  <kind>pump</kind>\n' +
		`  <task-id>${o.taskId ?? 'task-p'}</task-id>\n` +
		'  <tool-use-id>toolu_p</tool-use-id>\n' +
		'  <output-file>/tmp/p.txt</output-file>\n' +
		`  <status>${o.status ?? 'completed'}</status>\n` +
		`  <summary>${o.summary ?? 'A step'}</summary>\n` +
		'  <note>internal step note</note>\n' +
		`  <result>${o.result ?? 'stepped'}</result>\n` +
		'</task-notification>'
	);
}

describe('quiet-pump wake notifications', () => {
	it('parses a pump block with kind==="pump" and no usage', () => {
		const s = fold([ev('message.user', { id: 'srv-pump', content: pumpNotificationBlock() })]);
		expect(s.messages).toHaveLength(1);
		const data = s.messages[0].systemData as Record<string, unknown>;
		expect(data.kind).toBe('pump');
		expect(data.status).toBe('completed');
		expect(data.usage).toEqual({});
	});

	it('an own notification (no <kind>) parses as kind==="own"', () => {
		const s = fold([ev('message.user', { id: 'srv-own', content: taskNotificationBlock({}) })]);
		expect((s.messages[0].systemData as Record<string, unknown>).kind).toBe('own');
	});

	it('splits two concatenated pump blocks into two pump chips', () => {
		const content =
			pumpNotificationBlock({ taskId: 'p-a', summary: 'first' }) +
			'\n' +
			pumpNotificationBlock({ taskId: 'p-b', summary: 'second' });
		const s = fold([ev('message.user', { id: 'srv-mp', content })]);
		expect(s.messages).toHaveLength(2);
		expect(
			s.messages.every((m) => (m.systemData as Record<string, unknown>).kind === 'pump')
		).toBe(true);
	});

	it('suppresses a STREAMED "ok" ack after a pump card', () => {
		const s = fold([
			ev('turn.started', { turn_id: 'turn-p' }),
			ev('message.user', { id: 'srv-pump', content: pumpNotificationBlock() }),
			ev('assistant.text_delta', { text: 'ok' }),
			ev('turn.completed', {}),
		]);
		expect(s.messages).toHaveLength(1);
		expect(s.messages[0].systemSubtype).toBe('task_notification');
		expect(s.messages.some((m) => m.role === 'assistant')).toBe(false);
	});

	it('suppresses a CLOSED "ok" ack (no-delta replay shape, R8)', () => {
		const s = fold([
			ev('turn.started', { turn_id: 'turn-p' }),
			ev('message.user', { id: 'srv-pump', content: pumpNotificationBlock() }),
			ev('message.assistant', { id: 'a1', content: 'ok' }),
			ev('turn.completed', {}),
		]);
		expect(s.messages).toHaveLength(1);
		expect(s.messages.some((m) => m.role === 'assistant')).toBe(false);
	});

	it('suppresses an "Ok." ack (case + one trailing punctuation, R5a)', () => {
		const s = fold([
			ev('turn.started', { turn_id: 'turn-p' }),
			ev('message.user', { id: 'srv-pump', content: pumpNotificationBlock() }),
			ev('message.assistant', { id: 'a1', content: 'Ok.' }),
			ev('turn.completed', {}),
		]);
		expect(s.messages.some((m) => m.role === 'assistant')).toBe(false);
	});

	it('renders assistant text that exceeds the ack prefix (model acts)', () => {
		const s = fold([
			ev('turn.started', { turn_id: 'turn-p' }),
			ev('message.user', { id: 'srv-pump', content: pumpNotificationBlock() }),
			ev('assistant.text_delta', { text: 'okay, let me investigate that failure' }),
			ev('turn.completed', {}),
		]);
		const assistant = s.messages.filter((m) => m.role === 'assistant');
		expect(assistant).toHaveLength(1);
		expect(assistant[0].content).toBe('okay, let me investigate that failure');
	});

	it('reveals buffered ack-prefix text once a later delta exceeds it', () => {
		const s = fold([
			ev('turn.started', { turn_id: 'turn-p' }),
			ev('message.user', { id: 'srv-pump', content: pumpNotificationBlock() }),
			ev('assistant.text_delta', { text: 'ok' }),
			ev('assistant.text_delta', { text: ' — on it, fixing now' }),
			ev('turn.completed', {}),
		]);
		const assistant = s.messages.filter((m) => m.role === 'assistant');
		expect(assistant).toHaveLength(1);
		expect(assistant[0].content).toBe('ok — on it, fixing now');
	});

	it('a tool.use after a pump card clears the watch (trailing text renders)', () => {
		const s = fold([
			ev('turn.started', { turn_id: 'turn-p' }),
			ev('message.user', { id: 'srv-pump', content: pumpNotificationBlock() }),
			ev('tool.use', { tool_use_id: 'tool-1', name: 'Bash' }),
			ev('assistant.text_delta', { text: 'ok' }),
			ev('turn.completed', {}),
		]);
		expect(s.messages.some((m) => m.type === 'tool_use')).toBe(true);
		expect(s.messages.some((m) => m.role === 'assistant' && m.type === 'text')).toBe(true);
	});

	it('does not suppress "ok" when there was no pump card', () => {
		const s = fold([
			ev('turn.started', { turn_id: 'turn-x' }),
			ev('message.user', { id: 'u1', content: 'do the thing' }),
			ev('assistant.text_delta', { text: 'ok' }),
			ev('turn.completed', {}),
		]);
		const assistant = s.messages.filter((m) => m.role === 'assistant');
		expect(assistant).toHaveLength(1);
		expect(assistant[0].content).toBe('ok');
	});

	it('an own notification does NOT arm ack suppression', () => {
		const s = fold([
			ev('turn.started', { turn_id: 'turn-o' }),
			ev('message.user', { id: 'srv-own', content: taskNotificationBlock({}) }),
			ev('assistant.text_delta', { text: 'ok' }),
			ev('turn.completed', {}),
		]);
		expect(s.messages.some((m) => m.role === 'assistant' && m.content === 'ok')).toBe(true);
	});

	it('an own notification with a literal <kind>pump</kind> in its <result> stays OWN', () => {
		// The backend does NOT XML-escape summary/result text, so an own
		// notification whose result body contains a literal <kind>pump</kind>
		// must not be misclassified as pump (kind is read from the header only).
		const content = taskNotificationBlock({
			result: 'Investigated. The pump path uses <kind>pump</kind> internally.',
		});
		const s = fold([
			ev('turn.started', { turn_id: 'turn-o' }),
			ev('message.user', { id: 'srv-inj-result', content }),
		]);
		expect(s.messages).toHaveLength(1);
		const data = s.messages[0].systemData as Record<string, unknown>;
		expect(data.kind).toBe('own');
		expect(data.status).toBe('completed');
		expect(data.summary).toBe('Did the thing');
		expect(data.usage).toEqual({ subagent_tokens: 88851, tool_uses: 57, duration_ms: 312000 });
		expect(s.pendingPumpAck).toBe(null);
	});

	it('an own notification with a literal <kind>pump</kind> in its <summary> stays OWN', () => {
		const content = taskNotificationBlock({
			summary: 'Ran the <kind>pump</kind> step and finished',
		});
		const s = fold([
			ev('turn.started', { turn_id: 'turn-o' }),
			ev('message.user', { id: 'srv-inj-summary', content }),
		]);
		expect(s.messages).toHaveLength(1);
		const data = s.messages[0].systemData as Record<string, unknown>;
		expect(data.kind).toBe('own');
		expect(data.summary).toBe('Ran the <kind>pump</kind> step and finished');
		expect(s.pendingPumpAck).toBe(null);
	});

	it('suppresses a CLOSED whitespace-only assistant message (silent no-reply)', () => {
		const s = fold([
			ev('turn.started', { turn_id: 'turn-p' }),
			ev('message.user', { id: 'srv-pump', content: pumpNotificationBlock() }),
			ev('message.assistant', { id: 'a1', content: '   \n  ' }),
			ev('turn.completed', {}),
		]);
		expect(s.messages).toHaveLength(1);
		expect(s.messages.some((m) => m.role === 'assistant')).toBe(false);
		expect(s.pendingPumpAck).toBe(null);
		expect(s.pumpAckBuffer).toBe('');
	});

	it('turn.aborted clears an armed pump watch and its buffered ack-prefix', () => {
		const s = fold([
			ev('turn.started', { turn_id: 'turn-p' }),
			ev('message.user', { id: 'srv-pump', content: pumpNotificationBlock() }),
			ev('assistant.text_delta', { text: 'ok' }),
			ev('turn.aborted', { reason: 'interrupt' }),
		]);
		expect(s.pendingPumpAck).toBe(null);
		expect(s.pumpAckBuffer).toBe('');
	});
});

// ─── Assistant deltas ───────────────────────────────────────────────────────

describe('assistant deltas', () => {
	it('accumulates consecutive text deltas into ONE streaming message', () => {
		const s = fold([
			ev('turn.started'),
			ev('assistant.text_delta', { id: 'd1', text: 'Hel' }),
			ev('assistant.text_delta', { id: 'd2', text: 'lo' }),
		]);
		const texts = s.messages.filter((m) => m.type === 'text' && m.role === 'assistant');
		expect(texts).toHaveLength(1);
		expect(texts[0].content).toBe('Hello');
		expect(texts[0].streaming).toBe(true);
		expect(texts[0].id).toBe('text-d1'); // id derived from first delta
	});

	it('interleaved thinking closes the open text block and opens a new one', () => {
		const s = fold([
			ev('assistant.text_delta', { id: 'a', text: 'foo' }),
			ev('assistant.thinking_delta', { id: 'b', text: 'hmm' }),
			ev('assistant.text_delta', { id: 'c', text: 'bar' }),
		]);
		const kinds = s.messages.map((m) => `${m.type}:${m.content}`);
		expect(kinds).toEqual(['text:foo', 'thinking:hmm', 'text:bar']);
		// first text block finalized when thinking opened
		expect(s.messages[0].streaming).toBe(false);
	});
});

// ─── Tool cards ─────────────────────────────────────────────────────────────

describe('tool cards', () => {
	it('tool.use then tool.result renders one card that completes', () => {
		const s = fold([
			ev('tool.use', { tool_use_id: 't1', name: 'Read', input: { file: 'x' } }),
			ev('tool.result', { tool_use_id: 't1', content_preview: 'ok', is_error: false }),
		]);
		const card = s.messages.find((m) => m.toolId === 't1')!;
		expect(card.type).toBe('tool_use');
		expect(card.toolName).toBe('Read');
		expect(card.toolInput).toEqual({ file: 'x' });
		expect(card.toolResult).toBe('ok');
		expect(card.toolStatus).toBe('complete');
		expect(card.streaming).toBe(false);
	});

	it('input_delta before tool.use opens a placeholder that tool.use fills', () => {
		const s = fold([
			ev('tool.input_delta', { tool_use_id: 't2', partial_json: '{"a":' }),
			ev('tool.input_delta', { tool_use_id: 't2', partial_json: '1}' }),
			ev('tool.use', { tool_use_id: 't2', name: 'Bash' }),
		]);
		const cards = s.messages.filter((m) => m.toolId === 't2');
		expect(cards).toHaveLength(1);
		expect(cards[0].partialToolInput).toBe('{"a":1}');
		expect(cards[0].toolName).toBe('Bash');
	});

	it('tool.result for an unknown tool is ignored (no phantom card)', () => {
		const s = fold([ev('tool.result', { tool_use_id: 'ghost', content_preview: 'x' })]);
		expect(s.messages).toHaveLength(0);
	});

	// ── Image-rejection classification (#612) ──
	it('is_error image-too-large rejection swaps to the friendly message', () => {
		const s = fold([
			ev('tool.use', { tool_use_id: 't1', name: 'Read', input: { file_path: 'big.png' } }),
			ev('tool.result', {
				tool_use_id: 't1',
				is_error: true,
				content_preview:
					'API Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":"messages.0.content.1.image.source.base64: image exceeds 5 MB maximum: 7340032 bytes > 5242880 bytes"}}',
			}),
		]);
		const card = s.messages.find((m) => m.toolId === 't1')!;
		expect(card.toolStatus).toBe('error');
		expect(card.toolResult).toBe(
			'Image too large for the model to read (limit ~5 MB / 8000 px). Try a smaller or downscaled version.'
		);
	});

	it('is_error unrecognized rejection passes the raw preview through unchanged', () => {
		const raw = 'API Error: 500 something totally unexpected happened on the server';
		const s = fold([
			ev('tool.use', { tool_use_id: 't2', name: 'Read', input: { file_path: 'x' } }),
			ev('tool.result', { tool_use_id: 't2', is_error: true, content_preview: raw }),
		]);
		const card = s.messages.find((m) => m.toolId === 't2')!;
		expect(card.toolStatus).toBe('error');
		expect(card.toolResult).toBe(raw); // no substitution on a miss
	});

	it('is_error:false result is never classified even if it mentions an image', () => {
		const preview = 'Read image exceeds expectations — looks great';
		const s = fold([
			ev('tool.use', { tool_use_id: 't3', name: 'Read', input: { file_path: 'ok.png' } }),
			ev('tool.result', { tool_use_id: 't3', is_error: false, content_preview: preview }),
		]);
		const card = s.messages.find((m) => m.toolId === 't3')!;
		expect(card.toolStatus).toBe('complete');
		expect(card.toolResult).toBe(preview); // untouched passthrough
	});
});

// ─── Subagents (correlated by task_id) ──────────────────────────────────────

describe('subagent cards', () => {
	it('started/progress/completed fold into one card keyed by task_id', () => {
		const s = fold([
			ev('subagent.started', { task_id: 'task-1', agent_id: 'a1', description: 'do work' }),
			ev('subagent.progress', {
				task_id: 'task-1',
				kind: 'tool_use',
				payload: { name: 'Grep', tool_use_id: 'c1', input: { q: 'z' } },
			}),
			ev('subagent.progress', {
				task_id: 'task-1',
				kind: 'tool_result',
				payload: { tool_use_id: 'c1', output: 'found', is_error: false },
			}),
			ev('subagent.completed', { task_id: 'task-1', status: 'completed', result_preview: 'done' }),
		]);
		const cards = s.messages.filter((m) => m.type === 'subagent');
		expect(cards).toHaveLength(1);
		const card = cards[0];
		expect(card.id).toBe('subagent-task-1');
		expect(card.agentStatus).toBe('completed');
		expect(card.content).toBe('done');
		expect(card.agentChildren).toHaveLength(1);
		expect(card.agentChildren![0]).toMatchObject({ toolId: 'c1', toolResult: 'found', toolStatus: 'complete' });
	});

	it('progress/completed for an unknown task_id are ignored', () => {
		const s = fold([ev('subagent.progress', { task_id: 'nope', kind: 'chunk', note: 'x' })]);
		expect(s.messages).toHaveLength(0);
	});

	it('subagent.started drops the orphan launch placeholder opened by input_delta', () => {
		// The launch tool_use block emits input_delta (a spinning placeholder)
		// but never tool.use/tool.result. When the card arrives for the SAME id
		// (launch tool_use_id == task_id), the placeholder must be removed.
		const s = fold([
			ev('tool.input_delta', { tool_use_id: 'toolu-1', partial_json: '{"desc":' }),
			ev('subagent.started', { task_id: 'toolu-1', description: 'bg work' }),
		]);
		const cards = s.messages.filter((m) => m.type === 'subagent');
		expect(cards).toHaveLength(1);
		expect(cards[0].id).toBe('subagent-toolu-1');
		const orphan = s.messages.find((m) => m.type === 'tool_use' && m.toolId === 'toolu-1');
		expect(orphan).toBeUndefined();
	});

	it('input_delta after the subagent card exists does not recreate a placeholder', () => {
		const s = fold([
			ev('subagent.started', { task_id: 'toolu-1', description: 'bg work' }),
			ev('tool.input_delta', { tool_use_id: 'toolu-1', partial_json: '{"desc":' }),
		]);
		const cards = s.messages.filter((m) => m.type === 'subagent');
		expect(cards).toHaveLength(1);
		const placeholder = s.messages.find((m) => m.type === 'tool_use' && m.toolId === 'toolu-1');
		expect(placeholder).toBeUndefined();
	});

	it('subagent.progress re-opens a card that a premature terminal chip settled', () => {
		const s = fold([
			ev('subagent.started', { task_id: 'toolu-1', description: 'bg work' }),
			// A premature terminal chip settles the card to 'completed'.
			ev('task.updated', { task_id: 'reg-1', state: 'completed', tool_use_id: 'toolu-1' }),
			// Progress only flows while the agent works, so it must re-open.
			ev('subagent.progress', { task_id: 'toolu-1', kind: 'chunk', note: 'still going' }),
		]);
		const card = s.messages.find((m) => m.type === 'subagent');
		expect(card?.agentStatus).toBe('running');
		// A fresh terminal chip settles the re-opened card again.
		const s2 = reduce(s, ev('task.updated', { task_id: 'reg-1', state: 'completed', tool_use_id: 'toolu-1' }));
		const card2 = s2.messages.find((m) => m.type === 'subagent');
		expect(card2?.agentStatus).toBe('completed');
	});

	it('subagent.progress never re-opens a card settled to error', () => {
		const s = fold([
			ev('subagent.started', { task_id: 'toolu-1', description: 'bg work' }),
			ev('subagent.completed', { task_id: 'toolu-1', status: 'failed', result_preview: 'boom' }),
			ev('subagent.progress', { task_id: 'toolu-1', kind: 'chunk', note: 'late' }),
		]);
		const card = s.messages.find((m) => m.type === 'subagent');
		expect(card?.agentStatus).toBe('error');
	});
});

// ─── Task summaries ─────────────────────────────────────────────────────────

describe('task.updated summaries', () => {
	it('adds then updates a running task summary (default kind agent)', () => {
		const s = fold([
			ev('task.updated', { task_id: 'bg-1', state: 'running', description: 'crawl' }),
			ev('task.updated', { task_id: 'bg-1', state: 'completed', summary: 'ok' }),
		]);
		expect(s.activeTaskSummaries).toHaveLength(1);
		const t = s.activeTaskSummaries[0];
		expect(t.id).toBe('bg-1');
		expect(t.kind).toBe('agent');
		expect(t.status).toBe('completed');
		expect(t.subject).toBe('crawl'); // carried from the first update
		expect(t.summary).toBe('ok');
	});

	it('terminal task.updated settles the joined subagent card via tool_use_id', () => {
		// A background subagent's completion crossing a turn boundary arrives
		// ONLY as a task.updated chip (no subagent.completed is dispatched —
		// see _task_status_events_from_message). The card is keyed by the Task
		// tool_use_id; the registry-side task_id differs, so the join runs
		// through the summary's toolUseId captured on earlier updates.
		const s = fold([
			ev('subagent.started', { task_id: 'toolu-1', description: 'bg work' }),
			ev('task.updated', { task_id: 'reg-1', state: 'started', tool_use_id: 'toolu-1' }),
			ev('turn.parked', { task_ids: ['reg-1'] }),
			ev('task.updated', { task_id: 'reg-1', state: 'completed' }),
		]);
		const card = s.messages.find((m) => m.type === 'subagent');
		expect(card?.agentStatus).toBe('completed');
		expect(s.activeTaskSummaries[0].status).toBe('completed');
	});

	it('terminal task.updated with a failed state marks the card error', () => {
		const s = fold([
			ev('subagent.started', { task_id: 'toolu-1', description: 'bg work' }),
			ev('task.updated', { task_id: 'reg-1', state: 'failed', tool_use_id: 'toolu-1' }),
		]);
		const card = s.messages.find((m) => m.type === 'subagent');
		expect(card?.agentStatus).toBe('error');
	});

	it('non-terminal task.updated leaves the subagent card running', () => {
		const s = fold([
			ev('subagent.started', { task_id: 'toolu-1', description: 'bg work' }),
			ev('task.updated', { task_id: 'reg-1', state: 'running', tool_use_id: 'toolu-1' }),
		]);
		const card = s.messages.find((m) => m.type === 'subagent');
		expect(card?.agentStatus).toBe('running');
	});

	it('terminal task.updated never downgrades a settled card', () => {
		// subagent.completed carries the richer result; a late terminal chip
		// (drain-pump duplicate) must not overwrite it.
		const s = fold([
			ev('subagent.started', { task_id: 'toolu-1', description: 'bg work' }),
			ev('subagent.completed', { task_id: 'toolu-1', status: 'failed', result_preview: 'boom' }),
			ev('task.updated', { task_id: 'reg-1', state: 'completed', tool_use_id: 'toolu-1' }),
		]);
		const card = s.messages.find((m) => m.type === 'subagent');
		expect(card?.agentStatus).toBe('error');
		expect(card?.content).toBe('boom');
	});
});

// ─── Stale background work self-heal on turn.completed (#609) ────────────────

describe('turn.completed settles stale background work (#609)', () => {
	it('settles a subagent card left running when its terminal event was missed', () => {
		// The subagent.completed / terminal task.updated never arrives (the #543
		// wake-turn delivery gap). turn.completed only fires when no background work
		// remains (park gate), so the still-running card is stale → settle it.
		const s = fold([
			ev('subagent.started', { task_id: 'toolu-1', description: 'bg work' }),
			ev('turn.completed', {}),
		]);
		const card = s.messages.find((m) => m.type === 'subagent');
		expect(card?.agentStatus).toBe('completed');
	});

	it('finalizes streaming children of a stranded subagent card', () => {
		const s = fold([
			ev('subagent.started', { task_id: 'toolu-1', description: 'bg work' }),
			ev('subagent.progress', { task_id: 'toolu-1', kind: 'chunk', note: 'partial' }),
			ev('turn.completed', {}),
		]);
		const card = s.messages.find((m) => m.type === 'subagent');
		expect(card?.agentChildren?.every((c) => !c.streaming)).toBe(true);
	});

	it('clears a running task summary so the activeTasks strip empties', () => {
		const s = fold([
			ev('task.updated', { task_id: 'bg-1', state: 'running', description: 'crawl' }),
			ev('turn.completed', {}),
		]);
		expect(s.activeTaskSummaries[0].status).toBe('completed');
	});

	it('does not resurrect or downgrade an already-settled card', () => {
		const s = fold([
			ev('subagent.started', { task_id: 'toolu-1', description: 'bg work' }),
			ev('subagent.completed', { task_id: 'toolu-1', status: 'failed', result_preview: 'boom' }),
			ev('turn.completed', {}),
		]);
		const card = s.messages.find((m) => m.type === 'subagent');
		expect(card?.agentStatus).toBe('error'); // richer terminal result preserved
		expect(card?.content).toBe('boom');
	});

	it('is a no-op (identity-preserving) when nothing is stale', () => {
		const base = fold([ev('turn.started')]);
		const after = reduce(base, ev('turn.completed', {}));
		expect(after.messages).toBe(base.messages);
		expect(after.activeTaskSummaries).toBe(base.activeTaskSummaries);
	});
});

// ─── turn.parked reverts the premature turn.completed settle ─────────────────
// The backend emits turn.completed UNCONDITIONALLY when the pump drains, THEN
// evaluates the park gate and emits turn.parked if background work remains
// (session_runtime._finalize). Ordering in the log: task.updated(running) →
// turn.completed → turn.parked. So a turn.parked immediately following a
// turn.completed PROVES the #609 self-heal settled prematurely — revert it.

describe('turn.parked reverts a premature turn.completed settle', () => {
	it('restores a running task summary settled by turn.completed when turn.parked follows', () => {
		const s = fold([
			ev('turn.started'),
			ev('task.updated', { task_id: 'bg-1', state: 'running', description: 'crawl' }),
			ev('turn.completed', {}),
			ev('turn.parked', {}),
		]);
		// Queue strip visibility restored: deriveActiveTasks keeps status==='running'.
		expect(s.activeTaskSummaries[0].status).toBe('running');
	});

	it('restores a running subagent card settled by turn.completed when turn.parked follows', () => {
		const s = fold([
			ev('turn.started'),
			ev('subagent.started', { task_id: 'toolu-1', description: 'bg work' }),
			ev('turn.completed', {}),
			ev('turn.parked', {}),
		]);
		const card = s.messages.find((m) => m.type === 'subagent');
		expect(card?.agentStatus).toBe('running');
	});

	it('keeps the settle (stays completed) when NO turn.parked follows (#609 self-heal preserved)', () => {
		const s = fold([
			ev('turn.started'),
			ev('task.updated', { task_id: 'bg-1', state: 'running', description: 'crawl' }),
			ev('turn.completed', {}),
		]);
		expect(s.activeTaskSummaries[0].status).toBe('completed');
	});

	it('a genuine terminal task.updated before turn.parked wins (stays completed)', () => {
		const s = fold([
			ev('turn.started'),
			ev('task.updated', { task_id: 'bg-1', state: 'running', description: 'crawl' }),
			ev('turn.completed', {}),
			ev('task.updated', { task_id: 'bg-1', state: 'completed', summary: 'ok' }),
			ev('turn.parked', {}),
		]);
		expect(s.activeTaskSummaries[0].status).toBe('completed');
	});

	it('a genuine subagent.completed before turn.parked wins (card stays terminal)', () => {
		const s = fold([
			ev('turn.started'),
			ev('subagent.started', { task_id: 'toolu-1', description: 'bg work' }),
			ev('turn.completed', {}),
			ev('subagent.completed', { task_id: 'toolu-1', status: 'failed', result_preview: 'boom' }),
			ev('turn.parked', {}),
		]);
		const card = s.messages.find((m) => m.type === 'subagent');
		expect(card?.agentStatus).toBe('error');
		expect(card?.content).toBe('boom');
	});

	it('the settle record does not leak: a later bare turn.parked reverts nothing', () => {
		// turn.completed with something stale records it; a subsequent CLEAN
		// turn.completed (nothing stale) overwrites the record with an empty set,
		// so a later park cannot revive the earlier settle.
		const s = fold([
			ev('turn.started'),
			ev('task.updated', { task_id: 'bg-1', state: 'running', description: 'crawl' }),
			ev('turn.completed', {}), // settles bg-1, records it
			ev('turn.started'),
			ev('turn.completed', {}), // nothing stale → clears the record
			ev('turn.parked', {}),
		]);
		expect(s.activeTaskSummaries[0].status).toBe('completed');
	});
});

// ─── System + unknown ───────────────────────────────────────────────────────

describe('system + unknown events', () => {
	it('system.note renders a system message', () => {
		const s = fold([ev('system.note', { id: 'n1', subtype: 'info', data: { content: 'note!' } })]);
		expect(s.messages[0]).toMatchObject({ role: 'system', type: 'system', content: 'note!', systemSubtype: 'info' });
	});

	it('system.note background_tasks_changed produces NO transcript message (#638)', () => {
		// This CLI/SDK bookkeeping event drives the active-tasks strip only; its
		// raw payload must never reach the transcript as a system card.
		const s = fold([
			ev('turn.started'),
			ev('system.note', {
				id: 'btc1',
				subtype: 'background_tasks_changed',
				data: { type: 'system', uuid: 'c020e825', tasks: [{ task_id: 'behdrcouf', task_type: 'local_bash', description: 'Sleep for 45 seconds' }] },
			}),
		]);
		expect(s.messages).toHaveLength(0);
	});

	it('background_tasks_changed does not disturb activeTaskSummaries (fed by task.updated) (#638)', () => {
		// The active-tasks strip is fed from task.updated -> activeTaskSummaries,
		// NOT from the background_tasks_changed note. Filtering the note must
		// leave that state intact.
		const s = fold([
			ev('turn.started'),
			ev('task.updated', { task_id: 'bg-1', state: 'running', description: 'Sleep for 45 seconds' }),
			ev('system.note', {
				id: 'btc1',
				subtype: 'background_tasks_changed',
				data: { type: 'system', tasks: [{ task_id: 'bg-1' }] },
			}),
		]);
		// The note created no message.
		expect(s.messages).toHaveLength(0);
		// activeTaskSummaries still carries the running task from task.updated.
		expect(s.activeTaskSummaries).toHaveLength(1);
		expect(s.activeTaskSummaries[0]).toMatchObject({ id: 'bg-1', status: 'running' });
	});

	it('compaction_boundary renders a system marker', () => {
		const s = fold([ev('system.compaction_boundary', { id: 'cb1' })]);
		expect(s.messages[0]).toMatchObject({ systemSubtype: 'compaction_boundary' });
	});

	it('unknown event types are ignored', () => {
		const before = fold([ev('turn.started')]);
		const after = reduce(before, ev('totally.unknown', { foo: 1 }));
		expect(after.messages).toEqual(before.messages);
	});

	it('phase-1 non-goal events do not mutate the transcript', () => {
		const before = fold([ev('assistant.text_delta', { text: 'x' })]);
		let s = before;
		for (const t of ['turn.queued', 'interaction.requested', 'transcript.rewound', 'artifact.created']) {
			s = reduce(s, ev(t));
		}
		expect(s.messages).toEqual(before.messages);
	});
});

// ─── Replay determinism ─────────────────────────────────────────────────────

describe('replay determinism', () => {
	const log: WireEvent[] = [
		ev('turn.started'),
		ev('message.user', { id: 'u1', content: 'go', command_id: 'cmd-1' }),
		ev('assistant.text_delta', { id: 't1', text: 'Wor' }),
		ev('assistant.text_delta', { id: 't2', text: 'king' }),
		ev('tool.use', { tool_use_id: 'tool-1', name: 'Bash', input: { cmd: 'ls' } }),
		ev('tool.result', { tool_use_id: 'tool-1', content_preview: 'file.txt' }),
		ev('subagent.started', { task_id: 'k1', agent_id: 'a', description: 'sub' }),
		ev('subagent.completed', { task_id: 'k1', status: 'completed', result_preview: 'ok' }),
		ev('turn.completed', { usage: { input_tokens: 3 } }),
	];

	it('folding the same log twice yields deeply-equal state', () => {
		expect(reduceAll(log)).toEqual(reduceAll(log));
	});

	it('lastSeq tracks the highest seq seen', () => {
		const s = reduceAll(log);
		expect(s.lastSeq).toBe(Math.max(...log.map((e) => e.seq)));
	});

	it('is idempotent when re-reducing from an intermediate snapshot', () => {
		const full = reduceAll(log);
		const half = reduceAll(log.slice(0, 5));
		const resumed = log.slice(5).reduce(reduce, half);
		expect(resumed).toEqual(full);
	});
});

// ─── Closed assistant messages (message.assistant) ──────────────────────────

describe('message.assistant (closed append)', () => {
	it('two consecutive assistant events → two separate closed bubbles', () => {
		const s = fold([
			ev('message.assistant', { id: 'a1', content: 'first', kind: 'text' }),
			ev('message.assistant', { id: 'a2', content: 'second', kind: 'text' }),
		]);
		const bubbles = s.messages.filter((m) => m.role === 'assistant' && m.type === 'text');
		expect(bubbles).toHaveLength(2);
		expect(bubbles.map((m) => m.content)).toEqual(['first', 'second']);
		expect(bubbles.every((m) => m.streaming === false)).toBe(true);
		expect(s.openTextId).toBeNull();
	});

	it('does NOT merge into or reopen an open streamed message', () => {
		const s = fold([
			ev('turn.started'),
			ev('assistant.text_delta', { id: 'd1', text: 'streamed' }),
			ev('message.assistant', { id: 'a1', content: 'closed', kind: 'text' }),
		]);
		const texts = s.messages.filter((m) => m.type === 'text' && m.role === 'assistant');
		expect(texts).toHaveLength(2);
		expect(texts[0].content).toBe('streamed');
		expect(texts[0].streaming).toBe(false); // stream closed, NOT merged into
		expect(texts[1].content).toBe('closed');
		expect(texts[1].streaming).toBe(false);
		expect(s.openTextId).toBeNull();
	});

	it('maps kind=thinking, uuid→jsonlUuid, model→metadata, profile_id→profileId', () => {
		const s = fold([
			ev('message.assistant', {
				id: 'a1',
				content: 'hmm',
				kind: 'thinking',
				uuid: 'u-1',
				model: 'claude-x',
				profile_id: 'p-1',
			}),
		]);
		const m = s.messages[0];
		expect(m.type).toBe('thinking');
		expect(m.streaming).toBe(false);
		expect(m.jsonlUuid).toBe('u-1');
		expect(m.profileId).toBe('p-1');
		expect(m.metadata).toMatchObject({ model: 'claude-x' });
	});
});

// ─── Team-member cards (keyed by team_name + teammate_name) ──────────────────

describe('team-member cards', () => {
	it('started/progress/completed fold into one card keyed by team+teammate', () => {
		const s = fold([
			ev('team.member_started', {
				team_name: 'squad',
				teammate_name: 'alice',
				description: 'do X',
				prompt: 'go',
			}),
			ev('team.member_progress', {
				team_name: 'squad',
				teammate_name: 'alice',
				kind: 'tool_use',
				payload: { name: 'Read', tool_use_id: 'c1', input: { f: 'a' } },
			}),
			ev('team.member_progress', {
				team_name: 'squad',
				teammate_name: 'alice',
				kind: 'tool_result',
				payload: { tool_use_id: 'c1', output: 'ok', is_error: false },
			}),
			ev('team.member_completed', {
				team_name: 'squad',
				teammate_name: 'alice',
				status: 'completed',
				result_preview: 'done',
				usage: { total_tokens: 5 },
			}),
		]);
		const cards = s.messages.filter((m) => m.type === 'team_member');
		expect(cards).toHaveLength(1);
		const c = cards[0];
		expect(c.teamName).toBe('squad');
		expect(c.teammateName).toBe('alice');
		expect(c.agentDescription).toBe('do X');
		expect(c.agentPrompt).toBe('go');
		expect(c.teammateStatus).toBe('completed');
		expect(c.content).toBe('done');
		expect(c.agentUsage).toEqual({ total_tokens: 5 });
		expect(c.agentChildren).toHaveLength(1);
		expect(c.agentChildren![0]).toMatchObject({ toolId: 'c1', toolResult: 'ok', toolStatus: 'complete' });
	});

	it('status killed stays killed; failed maps to error', () => {
		const killed = fold([
			ev('team.member_started', { team_name: 't', teammate_name: 'k' }),
			ev('team.member_completed', { team_name: 't', teammate_name: 'k', status: 'killed' }),
		]);
		expect(killed.messages[0].teammateStatus).toBe('killed');
		const failed = fold([
			ev('team.member_started', { team_name: 't', teammate_name: 'f' }),
			ev('team.member_completed', { team_name: 't', teammate_name: 'f', status: 'failed' }),
		]);
		expect(failed.messages[0].teammateStatus).toBe('error');
	});

	it('two teammates on the same team produce two independent cards', () => {
		const s = fold([
			ev('team.member_started', { team_name: 'sq', teammate_name: 'a' }),
			ev('team.member_started', { team_name: 'sq', teammate_name: 'b' }),
		]);
		expect(s.messages.filter((m) => m.type === 'team_member')).toHaveLength(2);
	});

	it('progress/completed for an unknown teammate are ignored', () => {
		const s = fold([
			ev('team.member_progress', { team_name: 't', teammate_name: 'ghost', kind: 'chunk', note: 'x' }),
		]);
		expect(s.messages).toHaveLength(0);
	});
});

// ─── transcript.rewound (deterministic truncation) ──────────────────────────

describe('transcript.rewound', () => {
	function rewindLog(): WireEvent[] {
		return [
			ev('message.user', { id: 'u1', content: 'hi', command_id: null, seq: 1 }),
			ev('assistant.text_delta', { id: 'd1', text: 'reply', seq: 2 }),
			ev('turn.completed', { seq: 3 }),
			ev('message.user', { id: 'u2', content: 'again', command_id: null, seq: 4 }),
			ev('assistant.text_delta', { id: 'd2', text: 'reply2', seq: 5 }),
			ev('transcript.rewound', { from_seq: 4, seq: 6 }),
		];
	}

	it('removes messages created at seq >= from_seq, keeps earlier ones', () => {
		const s = fold(rewindLog());
		expect(s.messages.map((m) => m.content)).toEqual(['hi', 'reply']);
		// pointer to the truncated open streamed reply2 is cleared
		expect(s.openTextId).toBeNull();
	});

	it('removes activeTasks entries created at seq >= from_seq', () => {
		const s = fold([
			ev('task.updated', { task_id: 'bg-1', state: 'running', description: 'a', seq: 2 }),
			ev('task.updated', { task_id: 'bg-2', state: 'running', description: 'b', seq: 5 }),
			ev('transcript.rewound', { from_seq: 4, seq: 6 }),
		]);
		expect(s.activeTaskSummaries.map((t) => t.id)).toEqual(['bg-1']);
	});

	it('optimistic messages (no origin seq) are exempt from removal', () => {
		const optimistic: ChatMessage = {
			id: 'local-opt',
			role: 'user',
			type: 'text',
			content: 'pending',
			timestamp: '2026-07-12T00:00:00.000Z',
		};
		const seeded = { ...initialState(), messages: [optimistic] };
		let s = reduce(seeded, ev('assistant.text_delta', { id: 'd1', text: 'x', seq: 5 }));
		s = reduce(s, ev('transcript.rewound', { from_seq: 1, seq: 6 }));
		expect(s.messages.map((m) => m.id)).toEqual(['local-opt']);
	});

	it('malformed rewind without from_seq is a no-op', () => {
		const before = fold([ev('assistant.text_delta', { id: 'd1', text: 'x', seq: 2 })]);
		const after = reduce(before, ev('transcript.rewound', { seq: 3 }));
		expect(after.messages).toEqual(before.messages);
	});

	it('is replay-deterministic: full fold equals resume-from-snapshot', () => {
		const log = rewindLog();
		const full = reduceAll(log);
		const half = reduceAll(log.slice(0, 4));
		const resumed = log.slice(4).reduce(reduce, half);
		expect(resumed).toEqual(full);
		expect(reduceAll(log)).toEqual(reduceAll(log));
	});
});

// ─── Timestamp preference (payload timestamp over envelope created_at) ───────

describe('timestamp preference', () => {
	it('prefers a payload timestamp over the envelope created_at', () => {
		const s = fold([
			ev('message.assistant', {
				id: 'a1',
				content: 'x',
				created_at: '2026-07-12T00:00:00.000Z',
				timestamp: '2026-07-11T12:00:00.000Z',
			}),
		]);
		expect(s.messages[0].timestamp).toBe('2026-07-11T12:00:00.000Z');
	});

	it('falls back to created_at when no payload timestamp is present', () => {
		const s = fold([
			ev('message.assistant', { id: 'a2', content: 'y', created_at: '2026-07-12T00:00:00.000Z' }),
		]);
		expect(s.messages[0].timestamp).toBe('2026-07-12T00:00:00.000Z');
	});
});

// ─── tool.use screenshot mapping ────────────────────────────────────────────

describe('tool.use screenshot', () => {
	it('maps payload screenshot onto the tool card toolScreenshot field', () => {
		const s = fold([
			ev('tool.use', { tool_use_id: 't1', name: 'Chrome', screenshot: 'data:image/png;base64,ABC' }),
		]);
		const card = s.messages.find((m) => m.toolId === 't1')!;
		expect(card.toolScreenshot).toBe('data:image/png;base64,ABC');
	});
});
