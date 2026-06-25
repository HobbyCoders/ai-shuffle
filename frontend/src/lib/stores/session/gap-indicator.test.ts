/**
 * Tests for the gap-indicator derivation (the recurring "streaming indicator
 * not showing after tools" bug).
 *
 * The gap indicator fills the dead air while a turn is still streaming but no
 * visible element signals activity — most importantly the pause AFTER a tool
 * call completes and BEFORE the next assistant message, while the model thinks.
 *
 * The historical failure: the turn-start synthetic (an EMPTY streaming
 * assistant-text) gets re-appended to the tail by the WS-merge and, naively,
 * looked like live streaming — suppressing the dots and stealing the streaming
 * anchor. These tests pin the "phantom is transparent" contract.
 */

import { describe, expect, it } from 'vitest';
import type { ChatMessage } from '../types';
import {
	findStreamingAnchorIndex,
	isEmptyStreamingAssistantText,
	shouldRenderAssistantText,
	shouldShowGapIndicator
} from './gap-indicator';

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
const runningTool = (name = 'Bash') =>
	msg({ role: 'assistant', type: 'tool_use', toolName: name, toolStatus: 'running', streaming: false });
const completeTool = (name = 'Bash') =>
	msg({ role: 'assistant', type: 'tool_use', toolName: name, toolStatus: 'complete', streaming: false });
const streamingThinking = (content = 'hmm') =>
	msg({ role: 'assistant', type: 'thinking', content, streaming: true });
const runningSubagent = () =>
	msg({ role: 'assistant', type: 'subagent', agentStatus: 'running' });

const SHOWN = true; // thinking blocks shown (default)
const HIDDEN = false; // thinking blocks hidden by preference

describe('isEmptyStreamingAssistantText', () => {
	it('flags an empty streaming assistant text (the synthetic phantom)', () => {
		expect(isEmptyStreamingAssistantText(phantom())).toBe(true);
		expect(isEmptyStreamingAssistantText(phantom('   '))).toBe(true);
		// type omitted defaults to text
		expect(isEmptyStreamingAssistantText(msg({ role: 'assistant', streaming: true, content: '' }))).toBe(true);
	});

	it('does NOT flag a streaming text with real content', () => {
		expect(isEmptyStreamingAssistantText(streamingText('hello'))).toBe(false);
	});

	it('does NOT flag a finalized empty text, a user message, or non-text types', () => {
		expect(isEmptyStreamingAssistantText(finalText(''))).toBe(false);
		expect(isEmptyStreamingAssistantText(msg({ role: 'user', streaming: true, content: '' }))).toBe(false);
		expect(isEmptyStreamingAssistantText(streamingThinking(''))).toBe(false);
	});
});

describe('findStreamingAnchorIndex', () => {
	it('returns -1 when nothing is streaming', () => {
		expect(findStreamingAnchorIndex([userMsg(), finalText('done'), completeTool()])).toBe(-1);
	});

	it('anchors on a real streaming text', () => {
		const msgs = [userMsg(), completeTool(), streamingText('typing')];
		expect(findStreamingAnchorIndex(msgs)).toBe(2);
	});

	it('skips a trailing phantom and anchors on the real streaming message before it', () => {
		const msgs = [userMsg(), streamingThinking('reasoning'), phantom()];
		// thinking (index 1) is the real streaming message; phantom (2) is skipped
		expect(findStreamingAnchorIndex(msgs)).toBe(1);
	});

	it('returns -1 when the only streaming message is a phantom', () => {
		const msgs = [userMsg(), completeTool(), phantom()];
		expect(findStreamingAnchorIndex(msgs)).toBe(-1);
	});
});

describe('shouldRenderAssistantText', () => {
	// The empty phantom that is NOT the streaming anchor must be suppressed so the
	// chat never shows a bare "Principal Engineer • time" header with no body.
	it('suppresses an empty phantom that is not the streaming anchor', () => {
		expect(shouldRenderAssistantText('', false)).toBe(false);
		expect(shouldRenderAssistantText(undefined, false)).toBe(false);
	});

	it('renders the active streaming anchor even before its first delta', () => {
		// Anchor owns the inline cursor / placeholder dots; render it even empty.
		expect(shouldRenderAssistantText('', true)).toBe(true);
	});

	it('renders any message that has real content', () => {
		expect(shouldRenderAssistantText('hello', false)).toBe(true); // finalized text
		expect(shouldRenderAssistantText('typing', true)).toBe(true); // streaming text
	});
});

describe('shouldShowGapIndicator', () => {
	it('is false when the turn is not streaming', () => {
		expect(shouldShowGapIndicator([completeTool()], false, SHOWN)).toBe(false);
	});

	it('is false when there are no messages', () => {
		expect(shouldShowGapIndicator([], true, SHOWN)).toBe(false);
	});

	// ── THE BUG: dead air after a completed tool call ──────────────────────
	it('shows the dots after a completed tool while the model thinks', () => {
		const msgs = [userMsg(), finalText('Found #1...'), completeTool('Grep')];
		expect(shouldShowGapIndicator(msgs, true, SHOWN)).toBe(true);
	});

	it('shows the dots after a GROUP of completed tools (the screenshot case)', () => {
		const msgs = [userMsg(), finalText("I'll scope each task"), completeTool('Read'), completeTool('Grep'), completeTool('Glob')];
		expect(shouldShowGapIndicator(msgs, true, SHOWN)).toBe(true);
	});

	// ── THE PHANTOM: empty streaming synthetic re-appended at the tail ─────
	it('shows the dots when a phantom synthetic sits at the tail after a tool', () => {
		const msgs = [userMsg(), completeTool('Grep'), phantom()];
		expect(shouldShowGapIndicator(msgs, true, SHOWN)).toBe(true);
	});

	it('shows the dots when the tail is nothing but a phantom (turn start)', () => {
		const msgs = [userMsg(), phantom()];
		expect(shouldShowGapIndicator(msgs, true, SHOWN)).toBe(true);
	});

	it('shows the dots when a phantom trails a finalized text block', () => {
		const msgs = [userMsg(), finalText('answer'), phantom()];
		expect(shouldShowGapIndicator(msgs, true, SHOWN)).toBe(true);
	});

	// ── Already-visible activity: do NOT double up ─────────────────────────
	it('is false while a real text is streaming (inline cursor covers it)', () => {
		expect(shouldShowGapIndicator([userMsg(), streamingText('typing…')], true, SHOWN)).toBe(false);
	});

	it('is false while a tool is running (spinner covers it)', () => {
		expect(shouldShowGapIndicator([userMsg(), runningTool()], true, SHOWN)).toBe(false);
	});

	it('is false while a subagent is running/pending (its status covers it)', () => {
		expect(shouldShowGapIndicator([userMsg(), runningSubagent()], true, SHOWN)).toBe(false);
	});

	// ── Thinking blocks ────────────────────────────────────────────────────
	it('is false while a SHOWN thinking block streams (its header dots cover it)', () => {
		expect(shouldShowGapIndicator([userMsg(), completeTool(), streamingThinking()], true, SHOWN)).toBe(false);
	});

	it('shows the dots while a HIDDEN thinking block streams (renders nothing)', () => {
		expect(shouldShowGapIndicator([userMsg(), completeTool(), streamingThinking()], true, HIDDEN)).toBe(true);
	});

	it('shows the dots when a HIDDEN streaming thinking block is masked by a trailing phantom', () => {
		const msgs = [userMsg(), completeTool(), streamingThinking(), phantom()];
		expect(shouldShowGapIndicator(msgs, true, HIDDEN)).toBe(true);
		// And when shown, the thinking block's own indicator covers it.
		expect(shouldShowGapIndicator(msgs, true, SHOWN)).toBe(false);
	});

	it('shows the dots when a HIDDEN streaming thinking block is masked by a trailing streaming TEXT (SDK reorder)', () => {
		// live_session_store.append_thinking inserts the thinking block AHEAD of an
		// unfinalized streaming text (block_stop never cleared its streaming flag),
		// so the tail is a streaming text while the model is actually thinking.
		// Hidden thinking renders nothing → the dots are still needed.
		const msgs = [userMsg(), completeTool(), streamingThinking('reasoning'), streamingText('partial…')];
		expect(shouldShowGapIndicator(msgs, true, HIDDEN)).toBe(true);
		// Shown: the thinking block's own header dots cover it → no double indicator.
		expect(shouldShowGapIndicator(msgs, true, SHOWN)).toBe(false);
	});

	it('still suppresses dots for a plain streaming text tail with no thinking in the run', () => {
		// Guard against over-broadening: a streaming text with no thinking block in
		// the contiguous streaming run keeps its inline cursor and no gap dots.
		const msgs = [userMsg(), completeTool(), streamingText('typing…')];
		expect(shouldShowGapIndicator(msgs, true, HIDDEN)).toBe(false);
		expect(shouldShowGapIndicator(msgs, true, SHOWN)).toBe(false);
	});

	// ── Background tasks run CONCURRENTLY with main-turn streaming ──────────
	// A session keeps streaming (the main turn thinks + calls its own tools)
	// while a background/terminal task runs. The task count must NOT gate the
	// dots — that was the "indicator vanishes after a task starts" bug. The
	// "Running N task" panel is the separate indicator for the task itself.
	it('shows the dots in a main-turn think-gap even while a background task runs', () => {
		const msgs = [userMsg(), finalText('on it'), completeTool('Bash')];
		expect(shouldShowGapIndicator(msgs, true, SHOWN)).toBe(true);
	});

	it('still suppresses dots for a running tool/subagent regardless of tasks', () => {
		// The tail-shape checks (not the task count) own the "another indicator is
		// already visible" cases, so they keep working on their own.
		expect(shouldShowGapIndicator([userMsg(), runningTool()], true, SHOWN)).toBe(false);
		expect(shouldShowGapIndicator([userMsg(), runningSubagent()], true, SHOWN)).toBe(false);
	});
});
