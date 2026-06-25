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
 * `(messages, isStreaming, showThinkingBlocks)`.
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
 * Whether a text/undefined assistant message should render its bubble at all.
 *
 * The turn-start synthetic (an empty streaming assistant-text) is re-appended to
 * the tail during post-tool think-gaps and while parked on a subagent. When it
 * is NOT the streaming anchor it renders only a bare "Name • time" header — no
 * body, no inline cursor — which reads as a second, confusing streaming
 * indicator (the empty "Principal Engineer …" bubble in the chat). Suppress it
 * so the SINGLE activity signal is the bottom gap dots (shouldShowGapIndicator)
 * or the real streaming message's own inline cursor.
 *
 * Renders ⟺ the message has content, OR it is the active streaming anchor (which
 * owns the inline cursor / placeholder dots even before its first delta lands).
 *
 * @param content        The message's text content.
 * @param isStreamAnchor Whether this message is the streaming anchor for the turn
 *                       (i.e. messageIndex === findStreamingAnchorIndex(...)).
 */
export function shouldRenderAssistantText(
	content: string | undefined,
	isStreamAnchor: boolean
): boolean {
	return Boolean(content) || isStreamAnchor;
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
 * Returns true ⟺ the turn is streaming AND the last visible-activity-bearing
 * message does not already show its own indicator:
 *   - a non-empty streaming text shows an inline cursor            → no gap dots
 *   - a streaming thinking block (when shown) shows header dots    → no gap dots
 *   - a running tool shows a spinner                               → no gap dots
 *   - a running/pending subagent shows its own status             → no gap dots
 *   - anything else (completed tool, finalized text, user msg, …)  → gap dots
 *
 * A streaming thinking block that the SDK reordered to sit just BEFORE a
 * trailing streaming text block still governs the decision (hidden thinking →
 * gap dots, shown thinking → its own header dots), so a stale streaming text
 * tail can no longer hide an active hidden-thinking phase.
 *
 * Background tasks do NOT gate this indicator. A session streams concurrently
 * with running background/terminal tasks (the main turn keeps thinking and
 * calling its own tools while a task runs), so the count of active tasks says
 * nothing about whether the main turn is mid-think. The "Running N task" panel
 * is the SEPARATE, parallel indicator for those tasks; the gap dots track the
 * main turn's own streaming activity. (An earlier `activeTaskCount > 0` short-
 * circuit here assumed "task running ⇒ main turn parked", which wrongly hid the
 * dots for the entire lifetime of any background task.) When the main turn is
 * genuinely blocked on a tool/subagent, the running-tool / running-subagent
 * tail checks below still suppress the dots.
 *
 * Trailing empty streaming assistant-text (the phantom) is skipped so it can't
 * masquerade as live streaming and suppress the dots.
 *
 * @param messages          The current message list.
 * @param isStreaming       Whether the turn is actively streaming.
 * @param showThinkingBlocks Whether thinking blocks are shown (default true);
 *                          when false a streaming thinking block renders
 *                          nothing, so the gap dots are still needed.
 */
export function shouldShowGapIndicator(
	messages: ChatMessage[],
	isStreaming: boolean,
	showThinkingBlocks: boolean
): boolean {
	if (!isStreaming) return false;
	if (messages.length === 0) return false;

	// Skip trailing phantom messages. If the tail is nothing but phantoms there
	// is genuine dead air → show the dots.
	let i = messages.length - 1;
	while (i >= 0 && isEmptyStreamingAssistantText(messages[i])) i--;
	if (i < 0) return true;

	const last = messages[i];
	if (last.streaming) {
		// Is the model actually THINKING right now? A streaming thinking block
		// doesn't have to be the tail: the SDK can emit a text block and THEN
		// start thinking without finalising that text (block_stop doesn't clear
		// the streaming flag, and live_session_store.append_thinking inserts the
		// thinking block *ahead* of the still-streaming text so it renders above
		// it). That leaves a streaming TEXT tail while a streaming thinking block
		// sits just before it. Scan the contiguous trailing streaming run so the
		// indicator reflects the real activity, not the stale text tail.
		let j = i;
		let streamingThinking = false;
		while (j >= 0 && messages[j].streaming) {
			if (messages[j].type === 'thinking') {
				streamingThinking = true;
				break;
			}
			j--;
		}
		if (streamingThinking) {
			// Thinking hidden by user preference renders nothing → gap dots are
			// still needed; thinking shown → its header dots already cover it.
			return showThinkingBlocks === false;
		}
		// No thinking in the streaming run: the streaming tail (inline text
		// cursor / running tool / subagent) shows its own indicator.
		return false;
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
