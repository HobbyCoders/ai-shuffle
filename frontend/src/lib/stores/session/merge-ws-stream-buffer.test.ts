/**
 * Tests for mergeWsStreamBuffer (Phase 2 of the poll merge) — the "dead air
 * after tools" bug.
 *
 * Symptom: during a turn, after a tool call completes and while the model is
 * thinking before the next block (a post-tool think-gap), the chat showed NO
 * activity indicator at all — no bottom gap dots, no per-message cursor — for
 * many seconds. A page refresh rendered everything fine. It only happened when
 * the think-gap followed a TEXT block that streamed.
 *
 * Root cause: `wsStreamContent` (the live WS partial-text buffer) is cleared by
 * the WS layer ONLY at turn boundaries, never when a text block finalizes
 * mid-turn into a tool call. So once any text streams in a turn, the buffer
 * stays non-empty. During a post-tool think-gap the poller has no streaming
 * text, so the merge re-appended the turn-start synthetic with the STALE buffer
 * as its content — a NON-EMPTY streaming assistant-text at the tail. That is not
 * an empty phantom, so it suppressed the gap dots (gap-indicator.ts only treats
 * EMPTY streaming assistant-text as transparent) and rendered nothing the user
 * could perceive → total dead air.
 *
 * The fix (mergeWsStreamBuffer): drop the buffer the moment a finalized
 * assistant text already contains it, and source the think-gap synthetic's
 * content ONLY from the (now-genuinely-live-or-empty) buffer. These tests
 * exercise the real merge logic and chain its output into shouldShowGapIndicator
 * to pin the end-to-end indicator behavior.
 */

import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '../types';
import { mergeWsStreamBuffer } from './session-init';
import { shouldShowGapIndicator } from './gap-indicator';

let _id = 0;
function msg(partial: Partial<ChatMessage>): ChatMessage {
	return {
		id: partial.id ?? `m${_id++}`,
		role: partial.role ?? 'assistant',
		content: partial.content ?? '',
		...partial
	} as ChatMessage;
}

const userMsg = (content = 'do the thing') => msg({ role: 'user', type: 'text', content });
const streamingText = (content: string) =>
	msg({ role: 'assistant', type: 'text', content, streaming: true });
const finalText = (content: string) =>
	msg({ role: 'assistant', type: 'text', content, streaming: false });
/** The turn-start synthetic / SDK pre-content text: empty + streaming. */
const phantom = (content = '') => msg({ role: 'assistant', type: 'text', content, streaming: true });
const completeTool = (name = 'Bash') =>
	msg({ role: 'assistant', type: 'tool_use', toolName: name, toolStatus: 'complete', streaming: false });

/** A non-empty streaming assistant text sitting at the tail (the bug). */
function hasNonEmptyStreamingTextTail(messages: ChatMessage[]): boolean {
	const last = messages[messages.length - 1];
	if (!last) return false;
	return (
		last.role === 'assistant' &&
		(last.type === 'text' || last.type === undefined) &&
		last.streaming === true &&
		(last.content ?? '').trim() !== ''
	);
}

const SHOWN = true;

describe('mergeWsStreamBuffer', () => {
	// ── Case 1: think-gap after a streamed text block (THE BUG) ────────────
	it('drops a stale buffer so a post-tool think-gap stays a transparent phantom', () => {
		// Previous state still carries the turn-start synthetic (the WS-merge keeps
		// re-appending it) AND a stale wsStreamContent = "Hello world".
		const prior = [userMsg(), phantom()];
		// Poller data tail: finalized assistant text "Hello world" then a completed
		// tool_use. No streaming message anywhere.
		const pollerData = [userMsg(), finalText('Hello world'), completeTool('Grep')];

		const result = mergeWsStreamBuffer(pollerData, 'Hello world', prior, true);

		// The stale buffer must be dropped.
		expect(result.wsStreamContent).toBe('');
		// No non-empty trailing streaming assistant-text may leak in.
		expect(hasNonEmptyStreamingTextTail(result.messages)).toBe(false);
		// And the gap dots must show during the think-gap.
		expect(shouldShowGapIndicator(result.messages, true, 0, SHOWN)).toBe(true);
	});

	it('drops a stale buffer even when the finalized text has trailing content beyond the buffer', () => {
		// The finalized text can be a strict superset of the buffer (the buffer is
		// the prefix that streamed before more text arrived). startsWith still
		// recognizes it as stale.
		const prior = [userMsg(), phantom()];
		const pollerData = [userMsg(), finalText('Hello world, here is more'), completeTool('Grep')];

		const result = mergeWsStreamBuffer(pollerData, 'Hello world', prior, true);

		expect(result.wsStreamContent).toBe('');
		expect(hasNonEmptyStreamingTextTail(result.messages)).toBe(false);
		expect(shouldShowGapIndicator(result.messages, true, 0, SHOWN)).toBe(true);
	});

	// ── Case 2: WS-ahead-of-poller head-start (must be preserved) ──────────
	it('preserves genuinely-live WS-ahead text not yet finalized', () => {
		// wsStreamContent = "Hel" is live; it is NOT present as a finalized message.
		// Poller has no streaming text yet (WS is ahead of the poller).
		const prior = [userMsg(), phantom()];
		const pollerData = [userMsg(), completeTool('Read')];

		const result = mergeWsStreamBuffer(pollerData, 'Hel', prior, true);

		// The live text must still be shown — not dropped.
		expect(result.wsStreamContent).toBe('Hel');
		const last = result.messages[result.messages.length - 1];
		expect(last.role).toBe('assistant');
		expect(last.type).toBe('text');
		expect(last.streaming).toBe(true);
		expect(last.content).toBe('Hel');
		// A non-empty streaming text shows its own inline cursor → no gap dots.
		expect(shouldShowGapIndicator(result.messages, true, 0, SHOWN)).toBe(false);
	});

	it('renders the longer WS buffer into an existing poller streaming text (head-start)', () => {
		// Poller already has a streaming text "Hel"; the WS buffer is ahead at
		// "Hello". The buffer must overwrite the shorter poller content.
		const prior = [userMsg(), streamingText('Hel')];
		const pollerData = [userMsg(), streamingText('Hel')];

		const result = mergeWsStreamBuffer(pollerData, 'Hello', prior, true);

		const last = result.messages[result.messages.length - 1];
		expect(last.content).toBe('Hello');
		expect(last.streaming).toBe(true);
		expect(result.wsStreamContent).toBe('Hello');
		// A streaming text with real content shows its own cursor → no gap dots.
		expect(shouldShowGapIndicator(result.messages, true, 0, SHOWN)).toBe(false);
	});

	it('preserves a live WS-ahead partial that merely prefixes an OLDER finalized text', () => {
		// A new block's live partial ("Let me") happens to prefix an EARLIER
		// assistant text in the turn, but NOT the most recent finalized text. The
		// staleness check matches only the most recent finalized text, so this
		// genuinely-live buffer must be preserved (the WS-ahead head-start) — a
		// broader "any finalized text" match would drop it as false-stale.
		const prior = [userMsg(), phantom()];
		const pollerData = [
			userMsg(),
			finalText('Let me look into this'), // older block, shares the "Let me" prefix
			completeTool('Grep'),
			finalText('Done searching'), // most recent finalized text — does NOT share the prefix
			completeTool('Read')
		];

		const result = mergeWsStreamBuffer(pollerData, 'Let me', prior, true);

		// "Let me" is the new block just starting → must be shown, not dropped.
		expect(result.wsStreamContent).toBe('Let me');
		const last = result.messages[result.messages.length - 1];
		expect(last.role).toBe('assistant');
		expect(last.type).toBe('text');
		expect(last.streaming).toBe(true);
		expect(last.content).toBe('Let me');
		// A non-empty streaming text shows its own inline cursor → no gap dots.
		expect(shouldShowGapIndicator(result.messages, true, 0, SHOWN)).toBe(false);
	});

	// ── Case 3: tool-only think-gap (regression) ───────────────────────────
	it('introduces no stale synthetic for a tool-only think-gap (empty buffer)', () => {
		// No text ever streamed → wsStreamContent empty. Tail ends with a completed
		// tool_use. No synthetic exists in prior state either.
		const prior = [userMsg(), completeTool('Bash')];
		const pollerData = [userMsg(), completeTool('Bash')];

		const result = mergeWsStreamBuffer(pollerData, '', prior, true);

		expect(result.wsStreamContent).toBe('');
		expect(hasNonEmptyStreamingTextTail(result.messages)).toBe(false);
		// The gap dots must show.
		expect(shouldShowGapIndicator(result.messages, true, 0, SHOWN)).toBe(true);
	});

	it('appends a harmless empty phantom for a turn-start think-gap (empty buffer + empty synthetic)', () => {
		// Turn start: synthetic exists but is empty, buffer empty. The merge may
		// re-append the synthetic, but as an empty phantom (transparent).
		const prior = [userMsg(), phantom()];
		const pollerData = [userMsg(), completeTool('Bash')];

		const result = mergeWsStreamBuffer(pollerData, '', prior, true);

		expect(result.wsStreamContent).toBe('');
		expect(hasNonEmptyStreamingTextTail(result.messages)).toBe(false);
		expect(shouldShowGapIndicator(result.messages, true, 0, SHOWN)).toBe(true);
	});

	// ── Case 4: multiple sequential think-gaps don't perpetuate stale content
	it('does not re-introduce stale content across two consecutive think-gap merges', () => {
		// First merge: stale buffer "Hello world", finalized text present.
		const prior1 = [userMsg(), phantom()];
		const pollerData1 = [userMsg(), finalText('Hello world'), completeTool('Grep')];
		const merge1 = mergeWsStreamBuffer(pollerData1, 'Hello world', prior1, true);

		expect(merge1.wsStreamContent).toBe('');
		expect(hasNonEmptyStreamingTextTail(merge1.messages)).toBe(false);

		// Second merge uses merge1's output as the prior state (simulating the next
		// poll in the same think-gap). The buffer is now empty; the prior tail
		// holds at most an empty phantom. The stale "Hello world" must NOT return.
		const prior2 = merge1.messages;
		const pollerData2 = [userMsg(), finalText('Hello world'), completeTool('Grep')];
		const merge2 = mergeWsStreamBuffer(pollerData2, merge1.wsStreamContent, prior2, true);

		expect(merge2.wsStreamContent).toBe('');
		expect(hasNonEmptyStreamingTextTail(merge2.messages)).toBe(false);
		expect(
			merge2.messages.some(
				(m) => m.role === 'assistant' && m.streaming && (m.content ?? '').includes('Hello world')
			)
		).toBe(false);
		expect(shouldShowGapIndicator(merge2.messages, true, 0, SHOWN)).toBe(true);
	});

	// ── Turn end: buffer always cleared ─────────────────────────────────────
	it('clears the buffer and leaves messages untouched when the turn is not streaming', () => {
		const prior = [userMsg(), streamingText('Hel')];
		const pollerData = [userMsg(), finalText('Hello world')];

		const result = mergeWsStreamBuffer(pollerData, 'Hello', prior, false);

		expect(result.wsStreamContent).toBe('');
		// Non-streaming: the messages array is returned as-is (no synthetic merge).
		expect(result.messages).toBe(pollerData);
	});

	// ── No-synthetic safety: nothing to re-append ───────────────────────────
	it('does not append when there is no synthetic and the poller has no streaming text', () => {
		const prior = [userMsg(), completeTool('Bash')];
		const pollerData = [userMsg(), completeTool('Bash')];

		const result = mergeWsStreamBuffer(pollerData, '', prior, true);

		// Same array reference is fine; the important thing is no phantom appears.
		expect(result.messages.length).toBe(pollerData.length);
		expect(hasNonEmptyStreamingTextTail(result.messages)).toBe(false);
	});
});
