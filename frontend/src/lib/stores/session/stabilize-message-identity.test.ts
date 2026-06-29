/**
 * Tests for stabilizeMessageIdentity (Phase 3 of the poll merge) — the
 * "assistant messages vanish mid-stream but reappear on refresh" bug.
 *
 * Symptom: during a turn with several text blocks interleaved with tool calls
 * (text → tool → text → tool …), only the FIRST text block rendered live. The
 * tool calls all showed, but the later text blocks disappeared until a page
 * refresh brought them back.
 *
 * Root cause: a streaming turn injects a synthetic placeholder seeded with the
 * per-TURN `message_id` (ONE id for the whole turn). Each new streaming text
 * block position-matches the prior streaming text / synthetic, and because an
 * empty synthetic has zero-length content the prefix check matches anything, so
 * every distinct server `text-{uuid}` block was swapped onto that single
 * synthetic id. `displayItems` dedups by id and silently dropped all but the
 * first text. A cold load has no synthetic to collapse onto, so refresh fixed
 * it.
 *
 * The fix (stabilizeMessageIdentity): an existing client id may be adopted by
 * AT MOST ONE message per pass; any further match keeps the server's own unique
 * id. A final safety net guarantees no two output messages share an id.
 */

import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '../types';
import { stabilizeMessageIdentity } from './session-init';

let _id = 0;
function msg(partial: Partial<ChatMessage>): ChatMessage {
	return {
		id: partial.id ?? `m${_id++}`,
		role: partial.role ?? 'assistant',
		content: partial.content ?? '',
		...partial
	} as ChatMessage;
}

const user = (content = 'do the thing', id = 'user-1') =>
	msg({ id, role: 'user', type: 'text', content });
const finalText = (content: string, id: string) =>
	msg({ id, role: 'assistant', type: 'text', content, streaming: false });
const streamingText = (content: string, id: string) =>
	msg({ id, role: 'assistant', type: 'text', content, streaming: true });
/** The turn-start synthetic: empty + streaming, seeded with the per-turn id. */
const synthetic = (id: string) =>
	msg({ id, role: 'assistant', type: 'text', content: '', streaming: true });
const tool = (name: string, id: string, status: ChatMessage['toolStatus'] = 'complete') =>
	msg({ id, role: 'assistant', type: 'tool_use', toolName: name, toolStatus: status, streaming: false });

function ids(messages: ChatMessage[]): string[] {
	return messages.map((m) => m.id);
}
function hasUniqueIds(messages: ChatMessage[]): boolean {
	const seen = new Set<string>();
	for (const m of messages) {
		if (seen.has(m.id)) return false;
		seen.add(m.id);
	}
	return true;
}
function assistantTexts(messages: ChatMessage[]): ChatMessage[] {
	return messages.filter((m) => m.role === 'assistant' && m.type === 'text');
}

describe('stabilizeMessageIdentity', () => {
	// ── THE BUG: a new streaming text block must not collapse onto the
	//    per-turn synthetic id already claimed by an earlier block ────────────
	it('keeps a second text block distinct instead of collapsing onto the first block id', () => {
		// Prior client state: block 1 already absorbed the synthetic (id 0b49),
		// then a tool ran, then the synthetic was re-appended (still id 0b49,
		// empty) during the post-tool think-gap.
		const prior = [
			user(),
			finalText('Hello world', '0b49'),
			tool('Grep', 'tool-1'),
			synthetic('0b49') // re-appended synthetic, SAME per-turn id
		];
		// Server data once block 2 starts streaming: distinct text-{uuid} ids.
		const incoming = [
			user(),
			finalText('Hello world', 'text-A'),
			tool('Grep', 'tool-1'),
			streamingText('Let me check', 'text-B')
		];

		const { messages } = stabilizeMessageIdentity(incoming, prior);

		// All output ids must be unique — the whole point.
		expect(hasUniqueIds(messages)).toBe(true);
		// Both text blocks survive as DISTINCT messages.
		const texts = assistantTexts(messages);
		expect(texts.length).toBe(2);
		expect(texts[0].content).toBe('Hello world');
		expect(texts[1].content).toBe('Let me check');
		expect(texts[0].id).not.toBe(texts[1].id);
	});

	// ── Many blocks in one turn: nothing collapses ───────────────────────────
	it('preserves five distinct text blocks across a long text/tool turn', () => {
		// Prior state where (buggy) earlier collapse would have stamped several
		// blocks with the per-turn id 0b49. The guard must still output unique ids.
		const prior = [
			user(),
			finalText('one', '0b49'),
			tool('Read', 't1'),
			finalText('two', '0b49'),
			tool('Read', 't2'),
			finalText('three', '0b49'),
			tool('Read', 't3'),
			finalText('four', '0b49'),
			tool('Read', 't4'),
			synthetic('0b49')
		];
		const incoming = [
			user(),
			finalText('one', 'text-1'),
			tool('Read', 't1'),
			finalText('two', 'text-2'),
			tool('Read', 't2'),
			finalText('three', 'text-3'),
			tool('Read', 't3'),
			finalText('four', 'text-4'),
			tool('Read', 't4'),
			streamingText('five', 'text-5')
		];

		const { messages } = stabilizeMessageIdentity(incoming, prior);

		expect(hasUniqueIds(messages)).toBe(true);
		const texts = assistantTexts(messages);
		expect(texts.length).toBe(5);
		expect(texts.map((m) => m.content)).toEqual(['one', 'two', 'three', 'four', 'five']);
		// No two texts share an id.
		expect(new Set(texts.map((m) => m.id)).size).toBe(5);
	});

	// ── Intended behavior still works: first block absorbs the synthetic ──────
	it('lets the first real text block adopt the synthetic id (single match)', () => {
		const prior = [user(), synthetic('0b49')];
		const incoming = [user(), streamingText('Hello', 'text-A')];

		const { messages, changed } = stabilizeMessageIdentity(incoming, prior);

		expect(changed).toBe(true);
		// The first block adopts the stable client id to avoid render churn.
		const texts = assistantTexts(messages);
		expect(texts.length).toBe(1);
		expect(texts[0].id).toBe('0b49');
		expect(texts[0].content).toBe('Hello');
	});

	// ── Pre-existing duplicate ids in the incoming array are split ────────────
	it('splits a duplicate id that already arrived in the incoming array', () => {
		const prior = [user()];
		const incoming = [
			user(),
			finalText('A', 'dup'),
			finalText('B', 'dup') // second message with the same id
		];

		const { messages } = stabilizeMessageIdentity(incoming, prior);

		expect(hasUniqueIds(messages)).toBe(true);
		const texts = assistantTexts(messages);
		expect(texts.length).toBe(2);
		expect(texts.map((m) => m.content)).toEqual(['A', 'B']);
	});

	// ── Idempotency: a stable array passes through untouched ──────────────────
	it('is a no-op when nothing changed (returns the same reference)', () => {
		const stable = [user(), finalText('Hello', 'text-A'), tool('Bash', 't1')];
		const { messages, changed } = stabilizeMessageIdentity(stable, stable);

		expect(changed).toBe(false);
		expect(messages).toBe(stable);
	});

	// ── Normal id-swap still happens for a single genuine match ───────────────
	it('adopts the existing id when a finalized text settles to a new server id', () => {
		const prior = [user(), streamingText('Hello world', 'client-id')];
		const incoming = [user(), finalText('Hello world', 'server-id')];

		const { messages, changed } = stabilizeMessageIdentity(incoming, prior);

		expect(changed).toBe(true);
		const texts = assistantTexts(messages);
		expect(texts[0].id).toBe('client-id'); // adopted the stable client id
		expect(texts[0].streaming).toBe(false); // and took the server's finalized state
	});

	// ── Empty-input guards ───────────────────────────────────────────────────
	it('returns incoming unchanged when prior is empty', () => {
		const incoming = [user(), finalText('A', 'text-A')];
		const { messages, changed } = stabilizeMessageIdentity(incoming, []);
		expect(changed).toBe(false);
		expect(messages).toBe(incoming);
	});

	// ── Subagent identity still stabilizes (adopts existing id once) ──────────
	it('adopts the existing id for a matched subagent message', () => {
		const prior = [
			user(),
			msg({ id: 'sub-client', role: 'assistant', type: 'subagent', content: 'working', agentStatus: 'running', streaming: true })
		];
		const incoming = [
			user(),
			msg({ id: 'sub-server', role: 'assistant', type: 'subagent', content: 'working', agentStatus: 'running', streaming: true })
		];

		const { messages, changed } = stabilizeMessageIdentity(incoming, prior);

		expect(changed).toBe(true);
		expect(hasUniqueIds(messages)).toBe(true);
		const sub = messages.find((m) => m.type === 'subagent');
		expect(sub?.id).toBe('sub-client');
	});

	// ── Tool calls keep their own unique ids (never collapse) ────────────────
	it('never collapses distinct tool ids', () => {
		const prior = [user(), tool('Bash', 't1'), tool('Read', 't2')];
		const incoming = [user(), tool('Bash', 't1'), tool('Read', 't2')];

		const { messages } = stabilizeMessageIdentity(incoming, prior);
		expect(ids(messages)).toEqual(['user-1', 't1', 't2']);
		expect(hasUniqueIds(messages)).toBe(true);
	});
});

// ─── Rewind jsonlUuid carry-forward ──────────────────────────────────────────
// Regression for "rewind blocked on a just-sent message until reload": a live
// user message is optimistic (no jsonlUuid). The post-turn poll delivers the
// server copy WITH jsonlUuid (the rewind checkpoint id). The merge adopts the
// optimistic object for reference stability — and previously DROPPED the server
// jsonlUuid, so MessageActions saw `messageUuid == null` and refused to rewind
// ("missing its checkpoint id. Try reloading"). The adopted object must carry
// the server's jsonlUuid forward.
describe('stabilizeMessageIdentity — rewind jsonlUuid carry-forward', () => {
	it('carries the server jsonlUuid onto an adopted optimistic user message', () => {
		// Optimistic message id is `user-<ts>`; server copy has its own id + uuid.
		const prior = [msg({ id: 'user-1700000000000', role: 'user', type: 'text', content: 'hello 2' })];
		const incoming = [
			msg({
				id: 'srv-user',
				role: 'user',
				type: 'text',
				content: 'hello 2',
				jsonlUuid: 'caf2d784-b103-4c4a-a612-308d2e992828'
			})
		];

		const { messages, changed } = stabilizeMessageIdentity(incoming, prior);

		expect(changed).toBe(true);
		const u = messages.find((m) => m.role === 'user');
		expect(u?.jsonlUuid).toBe('caf2d784-b103-4c4a-a612-308d2e992828');
	});

	it('does not clobber a jsonlUuid the optimistic message already has', () => {
		const prior = [msg({ id: 'u1', role: 'user', type: 'text', content: 'hi', jsonlUuid: 'keep-me' })];
		const incoming = [msg({ id: 'u1', role: 'user', type: 'text', content: 'hi', jsonlUuid: 'other' })];

		const { messages } = stabilizeMessageIdentity(incoming, prior);

		const u = messages.find((m) => m.role === 'user');
		expect(u?.jsonlUuid).toBe('keep-me');
	});

	it('preserves the server jsonlUuid through the id-swap branch (assistant settle)', () => {
		const prior = [user(), streamingText('Answer', 'client-id')];
		const incoming = [
			user(),
			msg({ id: 'server-id', role: 'assistant', type: 'text', content: 'Answer', streaming: false, jsonlUuid: 'asst-uuid' })
		];

		const { messages } = stabilizeMessageIdentity(incoming, prior);

		const a = assistantTexts(messages)[0];
		expect(a.id).toBe('client-id'); // adopts the stable client id
		expect(a.jsonlUuid).toBe('asst-uuid'); // but keeps the server's rewind uuid
	});
});
