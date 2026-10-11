import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '$lib/stores/types';
import { handoffEpisodes, sameHandoffs, type HandoffEpisode } from './handoff-episodes';
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
