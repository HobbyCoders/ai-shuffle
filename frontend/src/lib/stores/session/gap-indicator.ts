/**
 * Gap-indicator derivation — the bottom-of-chat "..." dots that fill the dead
 * air while a turn is still streaming but no visible element is signalling
 * activity. The canonical gap is the pause AFTER a tool call completes and
 * BEFORE the next assistant message appears, while the model is thinking /
 * generating the next tool_use. Without this the chat looks frozen for many
 * seconds even though the turn is very much alive.
 *
 * Pure + framework-free so it can be unit-tested in isolation (mirrors
 * indicator-state.ts). The chat view derives the gap indicator from
 * `(messages, isStreaming, activeTaskCount, showThinkingBlocks)`.
 *
 * THE PHANTOM PROBLEM (the recurring "indicator not showing after tools" bug):
 * the turn-start synthetic streaming message (`ws-stream-…`, empty content) is
 * re-appended to the tail of the message list by the WS-merge for as long as no
 * real assistant text is streaming (see session-init.ts Phase 2). During a
 * post-tool think-gap that synthetic — an EMPTY streaming assistant-text — sits
 * at the tail and, naively, looks like "something is streaming". That both
 *   (a) suppressed the gap dots (its `streaming` flag short-circuited the
 *       "last message" check to false), and
 *   (b) stole the streaming anchor from any real streaming message,
 * leaving the user with no indicator (or only a bare "Name •••" header on an
 * empty bubble). With partial-message streaming on, an empty streaming text
 * renders no body, so it contributes nothing the user can perceive as progress.
 * The fix: treat trailing empty streaming assistant-text as TRANSPARENT in both
 * the gap decision and the anchor selection.
 */

import type { ChatMessage } from '../types';

/**
 * A "phantom" assistant message: an empty streaming assistant-text that
 * occupies the tail but renders nothing the user perceives as activity. This is
 * the turn-start synthetic (or the SDK's pre-content `start_assistant_message`)
 * before any real text has streamed. Treated as transparent everywhere so it
 * can neither suppress the gap dots nor steal the streaming anchor.
 */
export function isEmptyStreamingAssistantText(m: ChatMessage): boolean {
	return (
		m.role === 'assistant' &&
		(m.type === 'text' || m.type === undefined) &&
		m.streaming === true &&
		(m.content ?? '').trim() === ''
	);
}

/**
 * Index of the message that should own the single streaming indicator / cursor,
 * or -1 if none. Only ONE message shows the bouncing dots, otherwise a row of
 * them appears after every tool call. Trailing EMPTY streaming assistant-text
 * messages (the phantom) are skipped so the anchor lands on the real streaming
 * message — or on nothing (-1), which lets the bottom gap indicator take over
 * and keeps the phantom rendering invisibly (streaming=false → empty body).
 */
export function findStreamingAnchorIndex(messages: ChatMessage[]): number {
	for (let i = messages.length - 1; i >= 0; i--) {
		const m = messages[i];
		if (!m.streaming) continue;
		if (isEmptyStreamingAssistantText(m)) continue;
		return i;
	}
	return -1;
}

/**
 * Whether to render the bottom-of-chat gap indicator.
 *
 * Returns true ⟺ the turn is streaming, no background task is active, AND the
 * last visible-activity-bearing message does not already show its own
 * indicator:
 *   - a non-empty streaming text shows an inline cursor            → no gap dots
 *   - a streaming thinking block (when shown) shows header dots    → no gap dots
 *   - a running tool shows a spinner                               → no gap dots
 *   - a running/pending subagent shows its own status             → no gap dots
 *   - anything else (completed tool, finalized text, user msg, …)  → gap dots
 *
 * Active background tasks take priority (mirrors deriveIndicatorState): while
 * parked on a sub-agent the SDK keeps reporting isStreaming = true, but we want
 * the calm "waiting" indicator, never the bouncing gap dots.
 *
 * Trailing empty streaming assistant-text (the phantom) is skipped so it can't
 * masquerade as live streaming and suppress the dots.
 *
 * @param messages          The current message list.
 * @param isStreaming       Whether the turn is actively streaming.
 * @param activeTaskCount   Number of currently-running background tasks.
 * @param showThinkingBlocks Whether thinking blocks are shown (default true);
 *                          when false a streaming thinking block renders
 *                          nothing, so the gap dots are still needed.
 */
export function shouldShowGapIndicator(
	messages: ChatMessage[],
	isStreaming: boolean,
	activeTaskCount: number,
	showThinkingBlocks: boolean
): boolean {
	if (!isStreaming) return false;
	// Active tasks win: show the calm "waiting" indicator, not the gap dots.
	if (activeTaskCount > 0) return false;
	if (messages.length === 0) return false;

	// Skip trailing phantom messages. If the tail is nothing but phantoms there
	// is genuine dead air → show the dots.
	let i = messages.length - 1;
	while (i >= 0 && isEmptyStreamingAssistantText(messages[i])) i--;
	if (i < 0) return true;

	const last = messages[i];
	if (last.streaming) {
		// A streaming thinking block hidden by user preference renders nothing,
		// so the gap indicator is still needed; otherwise the inline cursor /
		// header dots on the streaming message already cover it.
		return last.type === 'thinking' && showThinkingBlocks === false;
	}
	if (last.type === 'tool_use' && last.toolStatus === 'running') return false;
	if (
		last.type === 'subagent' &&
		(last.agentStatus === 'running' || last.agentStatus === 'pending')
	) {
		return false;
	}
	return true;
}
