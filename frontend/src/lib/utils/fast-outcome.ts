/**
 * Fast mode on a Claude Code chat (issue 991).
 *
 * Claude Code reads the Fast setting once, when a turn starts. The worker tells
 * the toggle when that makes a change wait (`fast_applies` on the run-settings
 * answer), and records how each turn really ran in a `fast_outcome` note.
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

/** What the toggle says while the running turn keeps its speed. */
export function fastAppliesNotice(fastEnabled: boolean): string {
	return fastEnabled
		? 'This turn keeps running at standard speed. Fast mode starts with your next message.'
		: 'This turn keeps running on Fast. Fast mode stops with your next message.';
}
