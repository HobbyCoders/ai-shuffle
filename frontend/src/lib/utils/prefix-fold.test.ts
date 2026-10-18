import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '$lib/stores/types';
import { initialState, reduce, releaseMessages, type WireEvent } from '$lib/stores/session/v2/reducer';
import { createPrefixFold, PREFIX_FOLD_TAIL } from './prefix-fold';
import { createLatestTurnKeyTracker, latestTurnKey } from './work-log';
import { createTurnNavigationSignature, turnNavigationSignatureStep } from './transcript-navigation';

const msg = (id: string, over: Partial<ChatMessage> = {}): ChatMessage =>
	({ id, role: 'assistant', type: 'text', content: id, ...over }) as ChatMessage;

describe('createPrefixFold', () => {
	const ids = () => createPrefixFold<string>(() => '', (s, m) => `${s}${m.id},`, 2);
	const full = (messages: readonly ChatMessage[]) => messages.map(m => `${m.id},`).join('');

	it('equals a full scan through appends, tail rewrites, history edits, rewinds and switches', () => {
		const fold = ids();
		let messages = ['a', 'b', 'c', 'd', 'e'].map(id => msg(id));
		const check = () => expect(fold(messages)).toBe(full(messages));
		check();
		messages = [...messages, msg('f')]; check();                 // append
		messages = [...messages.slice(0, -1), msg('f2')]; check();   // tail rewrite
		messages = [msg('a2'), ...messages.slice(1)]; check();      // historical replacement
		messages = messages.slice(0, 2); check();                     // rewind below the checkpoint
		messages = [...messages, msg('g'), msg('h'), msg('i')]; check();
		messages = ['x', 'y'].map(id => msg(id)); check();          // another session
		messages = []; check();
		messages = [msg('z')]; check();
	});

	it('returns the cached answer for the same array and visits only the tail on a streaming fold', () => {
		let steps = 0;
		const fold = createPrefixFold<number>(() => 0, (n) => { steps++; return n + 1; }, 4);
		const history = Array.from({ length: 1000 }, (_, i) => msg(`m${i}`));
		expect(fold(history)).toBe(1000);
		steps = 0;
		expect(fold(history)).toBe(1000);
		expect(steps).toBe(0);
		let messages = history;
		for (let delta = 0; delta < 20; delta++) {
			messages = [...messages.slice(0, -1), msg(`tail-${delta}`)];
			expect(fold(messages)).toBe(1000);
		}
		expect(steps).toBe(20 * 4);
	});
});

/** Reducer-produced history, so sharing and copy-on-publish match real feeds. */
function nativeFeed(turns: number) {
	let state = initialState();
	let seq = 0;
	const emit = (type: string, fields: Record<string, unknown> = {}) => {
		seq++;
		state = reduce(state, { type, id: `e-${seq}`, seq, session_id: 's', created_at: '2026-10-01T00:00:00Z', ...fields } as WireEvent);
		return releaseMessages(state);
	};
	for (let t = 0; t < turns; t++) {
		emit('turn.started', { turn_id: `t-${t}` });
		emit('message.user', { content: `Question ${t}`, turn_id: `t-${t}` });
		emit('tool.use', { tool_use_id: `tool-${t}`, name: 'Read', input: { file_path: 'a.ts' } });
		emit('tool.result', { tool_use_id: `tool-${t}`, content_preview: 'ok' });
		emit('assistant.text_delta', { text: `Answer ${t}` });
		emit('turn.completed', { stop_reason: 'end_turn' });
	}
	return emit;
}

describe('streaming turn scans (#1282)', () => {
	it.each([400, 2000])('read only the tail window per delta at %i turns and match the full scans', (turns) => {
		const emit = nativeFeed(turns);
		const keyOf = createLatestTurnKeyTracker();
		const signatureOf = createTurnNavigationSignature();
		let messages = emit('turn.started', { turn_id: 'live' });
		messages = emit('message.user', { content: 'Continue', turn_id: 'live' });
		messages = emit('assistant.text_delta', { text: 'Start' });
		keyOf(messages); signatureOf(messages);
		const history = messages.slice(0, -2);
		let reads = 0;
		for (const message of history) {
			const content = message.content;
			Object.defineProperty(message, 'content', { configurable: true, get() { reads++; return content; } });
		}
		for (let delta = 0; delta < 20; delta++) {
			messages = emit('assistant.text_delta', { text: ` token-${delta}` });
			keyOf(messages); signatureOf(messages);
		}
		// Each tracker revisits at most the tail window per delta, whatever the
		// history size (a full scan reads every prompt twice per delta).
		expect(reads).toBeLessThanOrEqual(20 * 2 * PREFIX_FOLD_TAIL);
		expect(keyOf(messages)).toBe(latestTurnKey(messages));
		expect(signatureOf(messages)).toBe(messages.reduce(turnNavigationSignatureStep, ''));
		// The next prompt opens a newer turn; both trackers follow it.
		messages = emit('turn.completed', { stop_reason: 'end_turn' });
		messages = emit('message.user', { content: 'Next', turn_id: 'next' });
		expect(keyOf(messages)).toBe(latestTurnKey(messages));
		expect(keyOf(messages)).toBe(messages.at(-1)?.id);
		expect(signatureOf(messages)).toBe(messages.reduce(turnNavigationSignatureStep, ''));
	});
});
