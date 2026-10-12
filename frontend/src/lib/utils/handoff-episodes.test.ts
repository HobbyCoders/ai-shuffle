import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '$lib/stores/types';
import { beginHandoffPendingRead, handoffEpisodes, resetOwnerTransitions, sameHandoffs, settleByOwner, type HandoffEpisode, type HandoffPendingView } from './handoff-episodes';
import { pendingHandoff } from '$lib/components/chat/pending-handoff';

let seq = 0;
const call = (result: unknown, input: Record<string, unknown> = { worker: 'DesktopWorker', note: 'Finish the PR review' }, extra: Partial<ChatMessage> = {}): ChatMessage => ({
	id: `call-${++seq}`, role: 'assistant', type: 'tool_use', content: '', toolStatus: 'complete',
	toolName: 'mcp__fluid_bundled__mcp__builtins__HandOffSession', toolInput: input,
	toolResult: result as string, ...extra
});
/** A row as app/core/session_handoff.py `_record` writes it: data carries `content` too. */
const row = (status: string, content: string, data: Record<string, unknown> = {}): ChatMessage => ({
	id: `row-${++seq}`, role: 'system', type: 'system', content, systemSubtype: 'handoff',
	systemData: { status, worker: 'DesktopWorker', ...data, content }
});
/** A pending read sent now, after the move was seen. */
const fresh = (pending: HandoffPendingView['pending']): HandoffPendingView => ({ known: true, pending, seq: beginHandoffPendingRead() });
const said = (content: string): ChatMessage => ({ id: `said-${++seq}`, role: 'assistant', type: 'text', content });
const queued = JSON.stringify({ queued: true, worker: 'DesktopWorker', message: 'Hand-off to DesktopWorker is queued.' });

function only(messages: ChatMessage[]): { anchor: ChatMessage; episode: HandoffEpisode; absorbed: ReadonlySet<ChatMessage> } {
	const { byAnchor, absorbed } = handoffEpisodes(messages);
	expect(byAnchor.size).toBe(1);
	const [[anchor, episode]] = [...byAnchor];
	return { anchor, episode, absorbed };
}

describe('handoffEpisodes: one row per hand-off', () => {
	it('is empty without a hand-off', () => {
		const result = handoffEpisodes([said('hello')]);
		expect(result.byAnchor.size).toBe(0);
		expect(result.absorbed.size).toBe(0);
		expect(result.open).toBeNull();
	});

	it("walks the agent's hand-off through starting, sending and done on the tool row", () => {
		const tool = call(queued);
		const started = row('started', 'Handing off to DesktopWorker. Note: Finish the PR review', { note: 'Finish the PR review' });
		const moved = row('moved', 'Handed off to DesktopWorker. This chat continues there on the same branch.', { resume: 'send' });

		let r = only([tool, said('Wrapping up.')]);
		expect(r.anchor).toBe(tool);
		expect(r.episode).toMatchObject({ phase: 'starting', worker: 'DesktopWorker', by: 'agent', note: 'Finish the PR review' });

		r = only([tool, said('Wrapping up.'), started]);
		expect(r.anchor).toBe(tool);
		expect(r.episode).toMatchObject({ phase: 'sending', sent: true });
		expect([...r.absorbed]).toEqual([started]);

		r = only([tool, said('Wrapping up.'), started, moved]);
		expect(r.episode).toMatchObject({
			phase: 'done',
			outcome: 'Handed off to DesktopWorker. This chat continues there on the same branch.'
		});
		expect([...r.absorbed]).toEqual([started, moved]);
		expect(handoffEpisodes([tool, started, moved]).open).toBeNull();
	});

	it('a call still waiting for its result is a starting row that queued nothing yet', () => {
		const r = only([call(undefined, { worker: 'DesktopWorker' }, { toolStatus: 'running' })]);
		expect(r.episode).toMatchObject({ phase: 'starting', requesting: true, worker: 'DesktopWorker' });
	});

	it('settles on the failure, keeping the backend reason', () => {
		const failed = row('failed', 'Hand-off to DesktopWorker failed: push rejected');
		const r = only([call(queued), row('started', 'Handing off to DesktopWorker.'), failed]);
		expect(r.episode).toMatchObject({ phase: 'failed', sent: true, outcome: 'Hand-off to DesktopWorker failed: push rejected' });

		const timeout = only([call(queued), row('timeout', 'Hand-off to DesktopWorker cancelled: this chat was still busy after 30 minutes.')]);
		expect(timeout.episode).toMatchObject({ phase: 'failed', reason: 'timeout' });
		expect(timeout.episode.sent).toBeUndefined();

		const prompt = only([call(queued), row('failed', 'Hand-off to DesktopWorker cancelled. Answer the open question first.', { reason: 'open_prompt' })]);
		expect(prompt.episode).toMatchObject({ phase: 'cancelled', reason: 'open_prompt' });
	});

	it('folds a cancel, a repeat request and a refused call correctly', () => {
		const tool = call(queued);
		const again = call(JSON.stringify({ queued: true, worker: 'DesktopWorker', message: 'already queued' }));
		const cancel = call(JSON.stringify({ cancelled: true }), { cancel: true });
		const r = only([tool, again, cancel]);
		expect(r.anchor).toBe(tool);
		expect(r.episode).toMatchObject({ phase: 'cancelled', reason: 'agent' });
		expect([...r.absorbed]).toEqual([again, cancel]);

		// A refused call is its own settled row; the queued hand-off stands.
		const refused = call('Unknown worker "Nope"', { worker: 'Nope' }, { toolStatus: 'error' });
		const result = handoffEpisodes([tool, refused, row('started', 'Handing off to DesktopWorker.')]);
		expect(result.byAnchor.get(refused)).toMatchObject({ phase: 'refused', worker: 'Nope' });
		expect(result.byAnchor.get(tool)).toMatchObject({ phase: 'sending' });
	});

	it("reads the owner's queued move as its own row, owner-worded (#1819)", () => {
		const queuedRow = row('queued', 'Move to DesktopWorker queued for after the queued messages.', { by: 'owner', after: 'queued' });
		let r = only([said('working'), queuedRow]);
		expect(r.anchor).toBe(queuedRow);
		expect(r.episode).toMatchObject({ phase: 'starting', by: 'owner', afterQueued: true });

		r = only([queuedRow, row('started', 'Handing off to DesktopWorker.', { note: '' }), row('moved', 'Handed off to DesktopWorker. Open this chat there to continue on the same branch.')]);
		expect(r.episode).toMatchObject({ phase: 'done', by: 'owner' });

		r = only([queuedRow, row('cancelled', 'Queued move to DesktopWorker cancelled.', { by: 'owner' })]);
		expect(r.episode).toMatchObject({ phase: 'cancelled' });
	});

	it("attaches the new worker's notes about held messages to the settled row", () => {
		const r = only([
			call(queued),
			row('started', 'Handing off to DesktopWorker.'),
			row('moved', 'Handed off to DesktopWorker.'),
			row('held_failed', 'DesktopWorker did not take 1 message sent during the move: "ping".', { count: 1 })
		]);
		expect(r.episode.phase).toBe('done');
		expect(r.episode.followUps).toEqual(['DesktopWorker did not take 1 message sent during the move: "ping".']);
		expect(r.absorbed.size).toBe(3);
	});

	it('a record with no open hand-off starts its own row (anchor not loaded, restart)', () => {
		const started = row('started', 'Handing off to DesktopWorker.');
		const r = only([said('earlier history trimmed'), started]);
		expect(r.anchor).toBe(started);
		expect(r.episode.phase).toBe('sending');
	});

	it('matches by worker: a record for another worker does not land on the open row', () => {
		const tool = call(queued);
		const other = { ...row('failed', 'Hand-off to Unraid failed: x'), systemData: { status: 'failed', worker: 'Unraid', content: 'Hand-off to Unraid failed: x' } };
		const result = handoffEpisodes([tool, other]);
		expect(result.byAnchor.size).toBe(2);
		expect(result.byAnchor.get(tool)?.phase).toBe('starting');
	});

	it('a second hand-off after a settled one is a second row', () => {
		const first = call(queued);
		const second = call(JSON.stringify({ queued: true, worker: 'Unraid' }), { worker: 'Unraid' });
		const result = handoffEpisodes([first, row('moved', 'Handed off to DesktopWorker.'), second]);
		expect(result.byAnchor.get(first)?.phase).toBe('done');
		expect(result.byAnchor.get(second)).toMatchObject({ phase: 'starting', worker: 'Unraid' });
		expect(result.open?.worker).toBe('Unraid');
	});

	it('drops a "nothing queued" row that closes nothing', () => {
		const stray = row('cancelled', 'No move is queued for this chat.', { worker: '', by: 'owner', reason: 'nothing_queued' });
		const result = handoffEpisodes([said('hi'), stray]);
		expect(result.byAnchor.size).toBe(0);
		expect(result.absorbed.has(stray)).toBe(true);
	});

	it('reuse: equal episodes compare equal; a phase change does not', () => {
		const tool = call(queued);
		const a = handoffEpisodes([tool]);
		expect(sameHandoffs(a, handoffEpisodes([tool, said('x')]))).toBe(true);
		expect(sameHandoffs(a, handoffEpisodes([tool, row('started', 'Handing off to DesktopWorker.')]))).toBe(false);
	});
});

describe('the chip and the row agree', () => {
	const sequences: [string, ChatMessage[]][] = [
		['queued', [call(queued)]],
		['started', [call(queued), row('started', 's')]],
		['moved', [call(queued), row('started', 's'), row('moved', 'm')]],
		['failed', [call(queued), row('started', 's'), row('failed', 'f')]],
		['owner queued', [row('queued', 'q', { by: 'owner', after: 'turn' })]],
		['owner cancelled', [row('queued', 'q', { by: 'owner' }), row('cancelled', 'c', { by: 'owner' })]],
		['agent cancel', [call(queued), call(JSON.stringify({ cancelled: true }), { cancel: true })]]
	];
	for (const [name, messages] of sequences) {
		it(name, () => {
			const { open } = handoffEpisodes(messages);
			const pending = pendingHandoff(messages);
			if (!open) {
				expect(pending).toBeNull();
				return;
			}
			expect(pending?.worker).toBe(open.worker);
			expect(pending?.phase).toBe(open.phase === 'sending' ? 'moving' : 'queued');
		});
	}
});

describe('review fixes (#2304)', () => {
	it('P1: on the source worker, an owner CHANGE to the target settles a sending move', () => {
		resetOwnerTransitions();
		const tool = call(queued);
		const messages = [tool, said('Wrapping up.'), row('started', 'Handing off to DesktopWorker.')];
		const handoffs = handoffEpisodes(messages);
		expect(handoffs.open?.phase).toBe('sending');
		// First seen sending while this worker owns it: the baseline, nothing changes.
		expect(settleByOwner(handoffs, { deviceId: 'dev_unraid', deviceName: 'Unraid' }, 's1')).toBe(handoffs);
		expect(pendingHandoff(messages, { deviceId: 'dev_unraid', deviceName: 'Unraid' }, 's1')).toEqual({ worker: 'DesktopWorker', phase: 'moving' });
		// The session row then changes to the target: done, and the chip clears.
		const settled = settleByOwner(handoffs, { deviceId: 'dev_gaming', deviceName: 'DesktopWorker' }, 's1');
		expect(settled.byAnchor.get(tool)).toMatchObject({ phase: 'done', worker: 'DesktopWorker' });
		expect(settled.open).toBeNull();
		expect(settleByOwner(handoffs, { deviceId: 'dev_gaming', deviceName: 'DesktopWorker' }, 's1')).toBe(settled);
		expect(pendingHandoff(messages, { deviceId: 'dev_gaming', deviceName: 'DesktopWorker' }, 's1')).toBeNull();
		// A move only waiting is never settled by ownership.
		expect(settleByOwner(handoffEpisodes([tool]), { deviceName: 'DesktopWorker' }, 's1').open?.phase).toBe('starting');
	});

	it('P1: a stale row that already names the target does not settle (equality is not a transfer)', () => {
		resetOwnerTransitions();
		const tool = call(queued);
		const gaming = { deviceId: 'dev_gaming', deviceName: 'DesktopWorker' };
		// The row said DesktopWorker before the request: queued stays starting…
		expect(settleByOwner(handoffEpisodes([tool]), gaming, 's2').open?.phase).toBe('starting');
		// …and `started` with the same row stays sending, chip and all.
		const messages = [tool, row('started', 's')];
		expect(settleByOwner(handoffEpisodes(messages), gaming, 's2').open?.phase).toBe('sending');
		expect(settleByOwner(handoffEpisodes(messages), gaming, 's2').open?.phase).toBe('sending');
		expect(pendingHandoff(messages, gaming, 's2')).toEqual({ worker: 'DesktopWorker', phase: 'moving' });
	});

	it('P1: the destination hydrating a move without `moved` stays sending', () => {
		resetOwnerTransitions();
		// The destination owns the chat from the first moment it shows it.
		const messages = [call(queued), row('started', 's')];
		const here = { deviceId: 'dev_gaming', deviceName: 'DesktopWorker' };
		for (let i = 0; i < 3; i++) expect(settleByOwner(handoffEpisodes(messages), here, 's3').open?.phase).toBe('sending');
		// A row with no owner yet sets no baseline: the first real owner does.
		resetOwnerTransitions();
		expect(settleByOwner(handoffEpisodes(messages), null, 's4').open?.phase).toBe('sending');
		expect(settleByOwner(handoffEpisodes(messages), here, 's4').open?.phase).toBe('sending');
	});

	it('P2: filling in an owner field is not an owner change', () => {
		const messages = [call(queued), row('started', 's')];
		const sending = () => handoffEpisodes(messages);
		// Id first, then the same id with its name: enrichment.
		resetOwnerTransitions();
		expect(settleByOwner(sending(), { deviceId: 'dev_gaming' }, 'e1').open?.phase).toBe('sending');
		expect(settleByOwner(sending(), { deviceId: 'dev_gaming', deviceName: 'DesktopWorker' }, 'e1').open?.phase).toBe('sending');
		expect(pendingHandoff(messages, { deviceId: 'dev_gaming', deviceName: 'DesktopWorker' }, 'e1')).toEqual({ worker: 'DesktopWorker', phase: 'moving' });
		// Name first, then the id filled in: enrichment.
		resetOwnerTransitions();
		expect(settleByOwner(sending(), { deviceName: 'DesktopWorker' }, 'e2').open?.phase).toBe('sending');
		expect(settleByOwner(sending(), { deviceId: 'dev_gaming', deviceName: 'DesktopWorker' }, 'e2').open?.phase).toBe('sending');
		// The same id renamed is the same owner.
		resetOwnerTransitions();
		expect(settleByOwner(sending(), { deviceId: 'dev_gaming', deviceName: 'Old name' }, 'e3').open?.phase).toBe('sending');
		expect(settleByOwner(sending(), { deviceId: 'dev_gaming', deviceName: 'DesktopWorker' }, 'e3').open?.phase).toBe('sending');
		// A real change of id still settles, after an enriched baseline too.
		resetOwnerTransitions();
		expect(settleByOwner(sending(), { deviceName: 'Unraid' }, 'e4').open?.phase).toBe('sending');
		expect(settleByOwner(sending(), { deviceId: 'dev_unraid', deviceName: 'Unraid' }, 'e4').open?.phase).toBe('sending');
		expect(settleByOwner(sending(), { deviceId: 'dev_gaming', deviceName: 'DesktopWorker' }, 'e4').open).toBeNull();
	});

	it('#2308: a reload after the move (owned by the target, X no longer pending) is done', () => {
		resetOwnerTransitions();
		const messages = [row('queued', 'q', { by: 'owner', handoff_id: 'X' }), row('started', 's', { handoff_id: 'X' })];
		const gaming = { deviceId: 'dev_gaming', deviceName: 'DesktopWorker' };
		// Pending not read yet: equality proves nothing.
		expect(settleByOwner(handoffEpisodes(messages), gaming, 'h1', { known: false, pending: null }).open?.phase).toBe('sending');
		// The new owner reports nothing pending: done (target known only by name after a reload).
		const nothing = fresh(null);
		expect(settleByOwner(handoffEpisodes(messages), gaming, 'h1', nothing).open).toBeNull();
		expect(pendingHandoff(messages, gaming, 'h1', nothing)).toBeNull();
	});

	it('#2308: owned by the target while X is still pending and moving stays sending', () => {
		resetOwnerTransitions();
		const messages = [row('queued', 'q', { by: 'owner', handoff_id: 'X' }), row('started', 's', { handoff_id: 'X' })];
		const gaming = { deviceId: 'dev_gaming', deviceName: 'DesktopWorker' };
		settleByOwner(handoffEpisodes(messages), gaming, 'h2');
		const stillMoving = fresh({ handoff_id: 'X', target_id: 'dev_gaming', moving: true });
		for (let i = 0; i < 2; i++) {
			expect(settleByOwner(handoffEpisodes(messages), gaming, 'h2', stillMoving).open?.phase).toBe('sending');
		}
		expect(pendingHandoff(messages, gaming, 'h2', stillMoving)).toEqual({ worker: 'DesktopWorker', phase: 'moving' });
		// Another move pending (a different id) means X ended: done.
		expect(settleByOwner(handoffEpisodes(messages), gaming, 'h2', fresh({ handoff_id: 'Y', target_id: 'dev_other' })).open).toBeNull();
	});

	it('#2308: with the target device known, ids decide, not names', () => {
		resetOwnerTransitions();
		const messages = [row('queued', 'q', { by: 'owner', handoff_id: 'X' }), row('started', 's', { handoff_id: 'X' })];
		const unraid = { deviceId: 'dev_serverworker', deviceName: 'Unraid' };
		settleByOwner(handoffEpisodes(messages), unraid, 'h3');
		// Seen pending while this worker owns it: the target id is learned.
		expect(settleByOwner(handoffEpisodes(messages), unraid, 'h3', fresh({ handoff_id: 'X', target_id: 'dev_gaming', moving: true })).open?.phase).toBe('sending');
		// A device merely NAMED like the target, with no pending move, is not the target.
		expect(settleByOwner(handoffEpisodes(messages), { deviceId: 'dev_imposter', deviceName: 'DesktopWorker' }, 'h3', fresh(null)).open?.phase).toBe('sending');
	});

	it('#2308: live, the owner_device_id changing to the target settles even before pending is read again', () => {
		resetOwnerTransitions();
		const messages = [call(JSON.stringify({ queued: true, worker: 'DesktopWorker', handoff_id: 'X', message: 'queued' })), row('started', 's', { handoff_id: 'X' })];
		settleByOwner(handoffEpisodes(messages), { deviceId: 'dev_unraid', deviceName: 'Unraid' }, 'h4');
		const stale = fresh({ handoff_id: 'X', target_id: 'dev_gaming', moving: true });
		expect(settleByOwner(handoffEpisodes(messages), { deviceId: 'dev_unraid', deviceName: 'Unraid' }, 'h4', stale).open?.phase).toBe('sending');
		// user_session_updated: owner_device_id is now the target device.
		expect(settleByOwner(handoffEpisodes(messages), { deviceId: 'dev_gaming', deviceName: 'DesktopWorker' }, 'h4', stale).open).toBeNull();
	});

	it('round 7 P1: a pending read sent before the move was seen never settles it', () => {
		resetOwnerTransitions();
		const messages = [row('queued', 'q', { by: 'owner', handoff_id: 'X' }), row('started', 's', { handoff_id: 'X' })];
		const gaming = { deviceId: 'dev_gaming', deviceName: 'DesktopWorker' };
		// The move menu's status check, from before X existed, saw nothing pending.
		const before = fresh(null);
		expect(settleByOwner(handoffEpisodes(messages.slice(0, 1)), gaming, 'r1', before).open?.phase).toBe('starting');
		// The saved owner already names the target (stale): still sending.
		expect(settleByOwner(handoffEpisodes(messages), gaming, 'r1', before).open?.phase).toBe('sending');
		// A fresh read reporting X moving keeps it sending; one reporting nothing ends it.
		expect(settleByOwner(handoffEpisodes(messages), gaming, 'r1', fresh({ handoff_id: 'X', target_id: 'dev_gaming', moving: true })).open?.phase).toBe('sending');
		expect(settleByOwner(handoffEpisodes(messages), gaming, 'r1', fresh(null)).open).toBeNull();
	});

	it('round 7 P1: settling on "no longer pending" is not kept when a newer read reports X again', () => {
		resetOwnerTransitions();
		const messages = [row('queued', 'q', { by: 'owner', handoff_id: 'X' }), row('started', 's', { handoff_id: 'X' })];
		const gaming = { deviceId: 'dev_gaming', deviceName: 'DesktopWorker' };
		settleByOwner(handoffEpisodes(messages), gaming, 'r2');
		expect(settleByOwner(handoffEpisodes(messages), gaming, 'r2', fresh(null)).open).toBeNull();
		expect(settleByOwner(handoffEpisodes(messages), gaming, 'r2', fresh({ handoff_id: 'X', target_id: 'dev_gaming', moving: true })).open?.phase).toBe('sending');
	});

	it('round 8 P1: a read sent while the call was still running does not count for the move it then queued', () => {
		resetOwnerTransitions();
		const gaming = { deviceId: 'dev_gaming', deviceName: 'DesktopWorker' };
		const id = 'call-early';
		const running = call(undefined, { worker: 'DesktopWorker' }, { id, toolStatus: 'running', toolResult: undefined });
		// The saved owner already names DesktopWorker; the call has no result yet.
		expect(settleByOwner(handoffEpisodes([running]), gaming, 'r8').open?.requesting).toBe(true);
		// A status read answers: nothing pending (X does not exist yet).
		const early = fresh(null);
		expect(settleByOwner(handoffEpisodes([running]), gaming, 'r8', early).open?.phase).toBe('starting');
		// The call returns X, and X starts: still sending on that early answer.
		const queuedX = call(JSON.stringify({ queued: true, worker: 'DesktopWorker', handoff_id: 'X', message: 'queued' }), { worker: 'DesktopWorker' }, { id });
		const messages = [queuedX, row('started', 's', { handoff_id: 'X' })];
		expect(settleByOwner(handoffEpisodes(messages.slice(0, 1)), gaming, 'r8', early).open?.phase).toBe('starting');
		expect(settleByOwner(handoffEpisodes(messages), gaming, 'r8', early).open?.phase).toBe('sending');
		expect(pendingHandoff(messages, gaming, 'r8', early)).toEqual({ worker: 'DesktopWorker', phase: 'moving' });
		// Reload recovery still works: a read sent now, with X gone, ends it.
		expect(settleByOwner(handoffEpisodes(messages), gaming, 'r8', fresh(null)).open).toBeNull();
	});

	it('round 7 P2: a legacy confirmed cancel and its record are one row', () => {
		const q = row('queued', 'Move to DesktopWorker queued for when this turn ends.', { by: 'owner' });
		const cancel = call(JSON.stringify({ cancelled: true }), { cancel: true });
		const record = row('cancelled', 'Queued move to DesktopWorker cancelled.');
		const result = handoffEpisodes([q, cancel, record]);
		expect(result.byAnchor.size).toBe(1);
		expect(result.byAnchor.get(q)?.phase).toBe('cancelled');
		expect(result.byAnchor.get(q)?.reason).toBe('agent');
		expect(result.absorbed.has(cancel) && result.absorbed.has(record)).toBe(true);
		expect(result.open).toBeNull();
		// A newer move queued before that record still stands: the record confirms the old one.
		const b = row('queued', 'Move to DesktopWorker queued for when this turn ends.', { by: 'owner' });
		const late = handoffEpisodes([q, cancel, b, record]);
		expect(late.byAnchor.get(q)?.phase).toBe('cancelled');
		expect(late.byAnchor.get(b)?.phase).toBe('starting');
		expect(late.open).toBe(late.byAnchor.get(b));
		// A confirmed cancel with nothing loaded to settle, then its record: one row.
		const alone = handoffEpisodes([cancel, record]);
		expect(alone.byAnchor.size).toBe(1);
	});

	it('P1: a late cancel of a replaced move keeps the newer move pending (no id)', () => {
		const a = row('queued', 'Move to DesktopWorker queued for when this turn ends.', { by: 'owner' });
		const b = row('queued', 'Move to DesktopWorker queued for when this turn ends.', { by: 'owner' });
		const lateCancel = row('cancelled', 'Queued move to DesktopWorker cancelled.', { by: 'owner' });
		const messages = [a, b, lateCancel];
		const result = handoffEpisodes(messages);
		expect(result.byAnchor.get(a)?.phase).toBe('cancelled');
		expect(result.byAnchor.get(b)?.phase).toBe('starting');
		expect(result.open).toBe(result.byAnchor.get(b));
		expect(result.absorbed.has(lateCancel)).toBe(true);
		expect(pendingHandoff(messages)).toEqual({ worker: 'DesktopWorker', phase: 'queued' });
		// B's own cancel then settles B.
		const both = handoffEpisodes([...messages, row('cancelled', 'Queued move to DesktopWorker cancelled.', { by: 'owner' })]);
		expect(both.byAnchor.get(b)?.phase).toBe('cancelled');
		expect(both.open).toBeNull();
	});

	it('P1: with handoff ids, records group strictly by id', () => {
		const a = row('queued', 'q', { by: 'owner', handoff_id: 'A' });
		const b = row('queued', 'q', { by: 'owner', handoff_id: 'B' });
		const cancelA = row('cancelled', 'c', { by: 'owner', handoff_id: 'A' });
		const startedB = row('started', 's', { handoff_id: 'B' });
		const result = handoffEpisodes([a, b, startedB, cancelA]);
		expect(result.byAnchor.get(a)?.phase).toBe('cancelled');
		expect(result.byAnchor.get(b)).toMatchObject({ phase: 'sending', id: 'B' });
		expect(result.open?.id).toBe('B');
		// The agent's call carries the id in its result too.
		const tool = call(JSON.stringify({ queued: true, worker: 'DesktopWorker', handoff_id: 'C' }));
		const viaTool = handoffEpisodes([tool, row('started', 's', { handoff_id: 'C' }), row('moved', 'm', { handoff_id: 'C' })]);
		expect(viaTool.byAnchor.size).toBe(1);
		expect(viaTool.byAnchor.get(tool)?.phase).toBe('done');
	});

	it('P2: an unresolved cancel call does not settle the move', () => {
		const tool = call(queued);
		const started = row('started', 's');
		const cancelling = call(undefined, { cancel: true }, { toolStatus: 'running' });
		let r = handoffEpisodes([tool, started, cancelling]);
		expect(r.byAnchor.get(tool)).toMatchObject({ phase: 'sending', cancelling: true });
		expect(r.absorbed.has(cancelling)).toBe(true);
		expect(pendingHandoff([tool, started, cancelling])).toEqual({ worker: 'DesktopWorker', phase: 'moving' });
		// `cancelled: false`: the move had started and goes on.
		const refused = { ...cancelling, toolStatus: 'complete' as const, toolResult: JSON.stringify({ cancelled: false }) };
		r = handoffEpisodes([tool, started, refused]);
		expect(r.byAnchor.get(tool)?.phase).toBe('sending');
		expect(r.byAnchor.get(tool)?.cancelling).toBeUndefined();
		// Confirmed: settled.
		const confirmed = { ...cancelling, toolStatus: 'complete' as const, toolResult: JSON.stringify({ cancelled: true }) };
		expect(handoffEpisodes([tool, confirmed]).byAnchor.get(tool)?.phase).toBe('cancelled');
	});

	it('P2: an id with no loaded anchor never lands in another move\'s row', () => {
		const b = row('queued', 'q', { by: 'owner', handoff_id: 'B' });
		const heldA = row('held_failed', 'Unraid did not take 1 message.', { worker: 'Unraid', handoff_id: 'A' });
		let result = handoffEpisodes([b, heldA]);
		expect(result.byAnchor.get(b)?.followUps).toEqual([]);
		expect(result.absorbed.has(heldA)).toBe(false);
		// An unknown-id terminal record is its own settled row; the open move stands.
		const failedA = row('failed', 'Hand-off to DesktopWorker failed: x', { handoff_id: 'A' });
		result = handoffEpisodes([b, failedA]);
		expect(result.byAnchor.get(failedA)?.phase).toBe('failed');
		expect(result.byAnchor.get(b)?.phase).toBe('starting');
		expect(result.open).toBe(result.byAnchor.get(b));
		// Nor does it adopt an open move that has no id (partial history).
		const legacy = call(queued);
		const startedA = row('started', 's', { handoff_id: 'A' });
		result = handoffEpisodes([legacy, failedA, startedA]);
		expect(result.byAnchor.get(legacy)?.phase).not.toBe('failed');
		expect(result.absorbed.has(failedA)).toBe(false);
		// An unknown-id cancel call settles nothing.
		const cancelA = call(JSON.stringify({ cancelled: true, handoff_id: 'A' }), { cancel: true });
		result = handoffEpisodes([b, cancelA]);
		expect(result.byAnchor.get(b)?.phase).toBe('starting');
	});
});
