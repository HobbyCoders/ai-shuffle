/**
 * Fast mode on a Claude Code chat (issue 991).
 *
 * Claude Code reads the Fast setting once, when a turn starts. The worker tells
 * the toggle when that makes a change wait (`fast_applies_turn` on the run-settings
 * answer and on `/live`), and records how each turn really ran in a `fast_outcome` note.
 * Sentences about a finished turn come from the worker (app/core/fast_outcome.py).
 */

export const FAST_OUTCOME_SUBTYPE = 'fast_outcome';

export type FastOutcome = 'fast' | 'partial' | 'standard' | 'unknown';

export interface FastOutcomeNoteView {
	outcome: FastOutcome;
	text: string;
}

const OUTCOMES: readonly string[] = ['fast', 'partial', 'standard', 'unknown'];

/** The note for a `fast_outcome` system row, else null. */
export function fastOutcomeNoteView(
	subtype: string | null | undefined,
	data: Record<string, unknown> | null | undefined,
	fallbackContent?: string | null
): FastOutcomeNoteView | null {
	if (subtype !== FAST_OUTCOME_SUBTYPE) return null;
	const content = typeof data?.content === 'string' && data.content ? data.content : fallbackContent;
	if (!content) return null;
	const outcome = typeof data?.outcome === 'string' && OUTCOMES.includes(data.outcome) ? data.outcome : 'unknown';
	return { outcome: outcome as FastOutcome, text: content };
}

/**
 * Whether the toggle should say a change waits. The worker names the turn it is
 * waiting out, so the answer holds whatever order things reach this device in:
 * that turn ended, another began in the same feed flush, or the worker's answer
 * only arrived after the next turn had started.
 */
export function fastChangeWaits(state: {
	isStreaming: boolean;
	liveTurnId: string | null;
	fastAppliesTurnId?: string | null;
}): boolean {
	return state.isStreaming && !!state.fastAppliesTurnId && state.fastAppliesTurnId === state.liveTurnId;
}

/**
 * What the toggle says while a change waits. It speaks of the setting only: the
 * running turn may not be on the speed the setting suggests (Fast can be refused).
 */
export function fastAppliesNotice(fastEnabled: boolean): string {
	return `Fast mode turns ${fastEnabled ? 'on' : 'off'} with your next message. This turn finishes at its current speed.`;
}
